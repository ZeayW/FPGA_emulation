"""Removed combinational cones remain source-bound and completely observed."""
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.vivado_constant_frontier import prepare_constant_frontier_query, confirm_selected_constant_sources
from emuflow.vivado_lineage import import_cell_lineage, INVENTORY_HEADER
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_optimization import build_optimization_proofs
from emuflow.yosys import import_yosys_json

HEADER=['source_origin','native_cell_hex','ref','init','pin','driver_kind','driver_source','driver_ref',
        'driver_pin','driver_name_hex','driver_transformations_hex','transformations_hex','carry_type']


def constant_cone_model(tmp_path, *, port=False, prefix='', dynamic=False, live_count=1, upstream=False, expose_upstream=False, dead_carry=False):
    def ff(d, q, init='0', ce='1'):
        return dict(type='FDRE',parameters={'INIT':init},attributes={},
            port_directions={'C':'input','CE':'input','D':'input','R':'input','Q':'output'},
            connections={'C':[2],'CE':[ce],'D':[d],'R':['0'],'Q':[q]})
    cells={'conditional':ff('0',5,'x',3)}
    for i in range(live_count):cells[f'live{i}']=ff(100+i,200+i)
    if upstream:cells['other']=ff(100,7,'x','0')
    cells['adapter']=dict(type='LUT6_2',parameters={'INIT':('0110'*8 if dynamic=='xor'else '1100'*8)+'10'*16},attributes={},
        port_directions={**{f'I{i}':'input'for i in range(6)},'O5':'output','O6':'output'},
        connections={'I0':[5],'I1':[7 if upstream else 200 if dynamic else 5],'I2':['0'],'I3':['0'],'I4':['0'],'I5':['1'],'O5':[8],'O6':[9]})
    carry_inputs={'CI':['0'],'CI_TOP':['0'],'DI':[8]+['0']*7,'S':[9]+['0']*7}
    free=[('DI',i)for i in range(1,8)]+[('S',i)for i in range(1,8)]
    for n,(bus,bit)in enumerate(free[:live_count]):carry_inputs[bus][bit]=200+n
    if dead_carry:
        cells['dead']=dict(type='CARRY8',parameters={'CARRY_TYPE':'SINGLE_CY8'},attributes={},
            port_directions={'CI':'input','CI_TOP':'input','DI':'input','S':'input','CO':'output','O':'output'},
            connections={'CI':['0'],'CI_TOP':['0'],'DI':['0']*8,'S':[5]+['0']*7,
                         'CO':list(range(300,308)),'O':list(range(308,316))})
    if not port:
        cells['front']=dict(type='CARRY8',parameters={'CARRY_TYPE':'SINGLE_CY8'},attributes={},
            port_directions={**{p:'input'for p in carry_inputs},'CO':'output','O':'output'},
            connections={**carry_inputs,'CO':list(range(20,28)),'O':list(range(28,36))})
    source=tmp_path/'source.json'
    source.write_text(json.dumps({'modules':{'dut':{'attributes':{'top':'1'},'cells':cells,'netnames':{},
        'ports':{'clk':{'direction':'input','bits':[2]},'enable':{'direction':'input','bits':[3]},
            'data':{'direction':'input','bits':list(range(100,100+live_count))},
            'q':{'direction':'output','bits':([8,9]if port else list(range(28,36)))+([7]if expose_upstream else [])}}}}}))
    ir=tmp_path/'source.emuir.sqlite';import_yosys_json(source).save(ir)
    report=emit_vivado_mapped_verilog(ir,tmp_path/'source.v')
    mapped=Path(report['mapped_index']['path']);functions=Path(report['source_functions']['path']);digest=report['mapped_index']['sha256']
    with closing(sqlite3.connect(mapped))as db:origins=dict(db.execute('SELECT id,seq FROM cells'))
    inventory=tmp_path/'inventory.tsv'
    with inventory.open('w')as stream:
        rows=csv.writer(stream,delimiter='\t',lineterminator='\n');rows.writerow(INVENTORY_HEADER)
        for name in cells:
            if name not in {'conditional','adapter','other','dead'}:rows.writerow(['p/'+name,cells[name]['type'],f's{origins[name]}','','','','','',''])
    lineage=tmp_path/'lineage.sqlite';import_cell_lineage(mapped,functions,{'synthesized':inventory},lineage,mapped_sha256=digest,retain_stage='synthesized')
    proof=tmp_path/'proof.sqlite';build_optimization_proofs(mapped,lineage,proof,mapped_sha256=digest,stage='synthesized',fpga='fpga0')
    native=tmp_path/'native.tsv'
    with native.open('w')as stream:
        rows=csv.writer(stream,delimiter='\t',lineterminator='\n');rows.writerow(HEADER)
        if port:
            for bit in range(2):
                name=(prefix+'/'if prefix else '')+f'q[{bit}]'
                rows.writerow([f's{origins["adapter"]}',name.encode().hex(),'MODULE_OUTPUT'if prefix else'OUTPUT_PORT','','PORT','zero','','GND','G','constant'.encode().hex(),'','',''])
        else:
            for pin,bits in carry_inputs.items():
                for bit,net in enumerate(bits):
                    if isinstance(net,int)and net>=200:
                        i=net-200;kind,origin,ref,driver='source',f's{origins[f"live{i}"]}','FDRE',f'p/live{i}/Q'
                    else:kind,origin,ref,driver='zero','','GND','constant/G'
                    scalar=pin if len(bits)==1 else f'{pin}[{bit}]'
                    rows.writerow([f's{origins["front"]}','p/front'.encode().hex(),'CARRY8','',scalar,kind,origin,ref,'Q'if kind=='source'else'G',driver.encode().hex(),'','','SINGLE_CY8'])
    options=dict(lineage_path=lineage,mapped_sha256=digest,candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256='a'*64,
        fpga='fpga0',selected_sources=[f's{origins["conditional"]}'],source_hierarchy_prefix=prefix)
    return mapped,functions,proof,inventory,native,options,origins


def confirm(model):
    mapped,functions,proof,inventory,native,options,_=model
    return confirm_selected_constant_sources(mapped,functions,proof,native,inventory,**options)


@pytest.mark.parametrize('live_count',[1,14])
def test_removed_dual_lut_projects_into_every_carry_input_without_name_preservation(tmp_path,live_count):
    model=constant_cone_model(tmp_path,live_count=live_count)
    mapped,_,proof,inventory,_,options,origins=model
    plan=prepare_constant_frontier_query(mapped,proof,inventory,**options)
    assert len(plan['projected_constant_bits'])==2
    assert {value for bit,value in plan['projected_constant_bits']}=={0}
    assert plan['sources'][0]['frontier_sources']==[f's{origins["front"]}']
    checked=confirm(model)
    assert checked['frontiers'][0]['variables']==live_count
    assert checked['frontiers'][0]['assignments']==1<<live_count


def test_proven_dead_carry_fanout_does_not_require_a_fictitious_constant_projection(tmp_path):
    model=constant_cone_model(tmp_path,dead_carry=True)
    mapped,_,proof,inventory,_,options,origins=model
    plan=prepare_constant_frontier_query(mapped,proof,inventory,**options)
    assert plan['unobservable_fanout_sources']==[f's{origins["dead"]}']
    assert plan['sources'][0]['frontier_sources']==[f's{origins["front"]}']
    assert confirm(model)['status']=='pass'


@pytest.mark.parametrize('port',[False,True])
def test_unobservable_label_cannot_hide_a_retained_primitive_or_module_output(tmp_path,port):
    model=constant_cone_model(tmp_path,port=port)
    mapped,_,proof,inventory,_,options,origins=model
    with closing(sqlite3.connect(proof))as db,db:
        db.execute("INSERT INTO eliminated_sources VALUES(?,'unobservable-source-closure',NULL,NULL)",(origins['adapter'],))
    options['candidate_proofs_sha256']=file_sha256(proof)
    message='module output'if port else'unproved source|retained object'
    with pytest.raises(ValidationError,match=message):
        prepare_constant_frontier_query(mapped,proof,inventory,**options)


@pytest.mark.parametrize('prefix',['','mapped_partition'])
def test_removed_constant_cone_must_observe_every_top_or_hierarchical_output_bit(tmp_path,prefix):
    model=constant_cone_model(tmp_path,port=True,prefix=prefix)
    cert=confirm(model)
    assert len(cert['frontiers'])==2
    assert {x['object_kind']for x in cert['frontiers']}=={'module-output'if prefix else'output-port'}
    native=model[4];native.write_text('\n'.join(native.read_text().splitlines()[:-1])+'\n')
    with pytest.raises(ValidationError,match='output port observation coverage is incomplete'):confirm(model)


@pytest.mark.parametrize('damage',['mode','live-input','missing-input','unknown-input','wrong-constant','constant-ref'])
def test_carry_projection_rejects_changed_parameters_inputs_and_incomplete_observations(tmp_path,damage):
    model=constant_cone_model(tmp_path);path=model[4]
    with path.open()as stream:reader=csv.DictReader(stream,delimiter='\t');rows=list(reader)
    if damage=='mode':
        for row in rows:row['carry_type']='DUAL_CY4'
    elif damage=='live-input':
        row=next(row for row in rows if row['driver_kind']=='source');row.update(driver_kind='zero',driver_source='',driver_ref='GND',driver_pin='G')
    elif damage=='missing-input':rows.pop()
    elif damage=='unknown-input':rows[0].update(driver_kind='opaque',driver_name_hex='unknown'.encode().hex())
    elif damage=='constant-ref':rows[0]['driver_ref']='VCC'
    else:rows[0].update(driver_kind='one',driver_ref='VCC',driver_pin='P')
    with path.open('w')as stream:writer=csv.DictWriter(stream,fieldnames=HEADER,delimiter='\t',lineterminator='\n');writer.writeheader();writer.writerows(rows)
    with pytest.raises(ValidationError):confirm(model)


def test_a_dynamic_removed_output_is_not_discarded_or_assumed_constant(tmp_path):
    model=constant_cone_model(tmp_path,dynamic='xor')
    with pytest.raises(ValidationError,match='not constant; native function observation is required'):confirm(model)


def test_unaffected_dynamic_adapter_output_keeps_its_live_carry_binding(tmp_path):
    model=constant_cone_model(tmp_path,dynamic=True);path=model[4];origins=model[6]
    with path.open()as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    row=next(row for row in rows if row['pin']=='S[0]')
    row.update(driver_kind='source',driver_source=f's{origins["live0"]}',driver_ref='FDRE',driver_pin='Q',driver_name_hex='p/live0/Q'.encode().hex())
    with path.open('w')as stream:writer=csv.DictWriter(stream,fieldnames=HEADER,delimiter='\t',lineterminator='\n');writer.writeheader();writer.writerows(rows)
    mapped,_,proof,inventory,_,options,_=model
    plan=prepare_constant_frontier_query(mapped,proof,inventory,**options)
    assert len(plan['projected_constant_bits'])==1
    assert confirm(model)['frontiers'][0]['variables']==1


def test_retained_primitive_inputs_cannot_borrow_unselected_initialization_choices(tmp_path):
    model=constant_cone_model(tmp_path,upstream=True)
    cert=confirm(model);origins=model[6]
    assert {row['source']for row in cert['sources']}=={f's{origins["conditional"]}',f's{origins["other"]}'}


def test_additional_frontier_state_choice_keeps_all_of_its_observable_exits(tmp_path):
    model=constant_cone_model(tmp_path,upstream=True,expose_upstream=True)
    with pytest.raises(ValidationError,match='output port observation coverage is incomplete'):confirm(model)


def test_frontier_membership_cannot_come_from_another_native_stage(tmp_path):
    model=constant_cone_model(tmp_path);lineage=model[5]['lineage_path']
    with closing(sqlite3.connect(lineage))as db,db:db.execute("UPDATE metadata SET value='routed' WHERE key='retained_stage'")
    with pytest.raises(ValidationError,match='matching preplacement source model'):confirm(model)
