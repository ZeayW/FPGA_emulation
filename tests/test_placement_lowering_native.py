"""Whole native lowering against the independent logical merge and identities."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.boundary_timing import build_boundary_identity_database
from emuflow.boundary_identity_storage import read_boundary_identity_storage
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.lowering import build_placement_ir,run_placement_ir_lowering_from_split
from emuflow.netlist import transport_endpoint_port
from emuflow.split_netlist_storage import read_split_netlist_sqlite
from emuflow.split_overlay import build_native_split
from emuflow.transport_storage import write_transport_storage
from tests.test_split_overlay_native import owners


def synthesized_transport(transport,shared_port=None):
    ports=[];cells=[];nets=[]
    def cell(name,kind):
        cells.append({'id':name,'name':name,'type':kind,'resources':{'ff' if kind=='FDRE' else 'lut':1},
                      'parameters':{'INIT':'0'},'attributes':{'note':'保留'},'constant_connections':[]})
    for kind,port,direction in [('source','source_values','input'),('shadow','shadow_values','output')]:
        signals=transport[kind+'_signals']
        if signals:ports.append({'id':port,'name':port,'width':len(signals),'direction':direction,'clock':False,'reset':False})
        for signal in signals:
            name=kind+str(signal['index']);cell(name,'LUT1' if kind=='source' else 'FDRE')
            top={'instance':None,'port':port,'bit':signal['index']}
            internal={'instance':name,'port':'I0' if kind=='source' else 'Q','bit':0,'tag':signal['index']}
            nets.append({'id':name,'name':name,'drivers':[top if kind=='source' else internal],
                         'sinks':[internal if kind=='source' else top],'fanout':1,'cut_class':'combinational',
                         'aliases':['alias-'+name]})
    external={}
    for endpoint in transport['endpoints']:
        port=transport_endpoint_port(endpoint);key=port,endpoint['lane'];is_tx=endpoint['kind']=='tx'
        record=external.setdefault(port,{'id':port,'name':port,'direction':'output' if is_tx else 'input',
                                        'width':0,'clock':False,'reset':False})
        record['width']=max(record['width'],endpoint['lane']+1)
        if any(net['id']==f'{port}:{key[1]}' for net in nets):continue
        top={'instance':None,'port':port,'bit':key[1]}
        nets.append({'id':f'{port}:{key[1]}','name':f'{port}:{key[1]}','cut_class':'combinational',
                     'drivers':[] if is_tx else [top],'sinks':[top] if is_tx else [],'fanout':int(is_tx)})
    ports.extend(external.values())
    if shared_port is not None:
        ports.append(deepcopy(shared_port))
        # Multiple destinations on one shared clock exercise the unique-DUT-net
        # contract independently of the number of added endpoint rows.
        for index in range(3):cell('clock_sink'+str(index),'FDRE')
        nets.append({'id':'shared-clock','name':'shared-clock','cut_class':'clock','fanout':3,
                     'drivers':[{'instance':None,'port':shared_port['id'],'bit':0}],
                     'sinks':[{'instance':'clock_sink'+str(i),'port':'C','bit':0,'tag':i} for i in range(3)]})
    return EmuIR({'schema':'emuflow.emuir/v1','design':{'name':'transport','top':'transport','source_format':'test'},
                  'ports':ports,'instances':cells,'nets':nets,'clocks':[],'warnings':['preserve warning']})


def prepare(owners,root,shared=False,fpga_index=0):
    case,source,assignment,schedule=owners
    if fpga_index>=len(case.platform.fpgas):pytest.skip('partition is absent')
    if shared:
        from emuflow.phase3_assignment_index import write_phase3_assignment_index
        value=deepcopy(case.ir.value)
        value['ports'].append({'id':'shared_reset','name':'shared_reset','direction':'input','width':1,'clock':False,'reset':True})
        value['nets'].append({'id':'shared_reset','name':'shared_reset','cut_class':'reset',
            'drivers':[{'instance':None,'port':'shared_reset','bit':0}],
            'sinks':[{'instance':c['id'],'port':'R','bit':0} for c in value['instances'] if c['type']=='FDRE']})
        value['nets'][-1]['fanout']=len(value['nets'][-1]['sinks'])
        case.ir=EmuIR(value);write_compact_emuir(source,value)
        assigned=deepcopy(case._phase5_assignment)
        declared={row['fpga'] for row in assigned['partitions']}
        assigned['partitions'].extend({'fpga':f.id,'resources':{}} for f in case.platform.fpgas if f.id not in declared)
        write_phase3_assignment_index(assignment,case.ir,assigned)
    split=root/'split.sqlite'
    build_native_split(split,source,assignment,schedule,expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    artifacts=write_transport_storage(root/'transport',assignment,schedule,case.platform,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'])
    fpga=case.platform.fpgas[fpga_index].id;transport=artifacts['transports'][fpga]
    logical=read_split_netlist_sqlite(split,fpga)
    port=next((p for p in logical['ports'] if p['id']=='shared_reset'),None) if shared else None
    if shared and port is None:pytest.skip('partition has no shared DUT input')
    ir=synthesized_transport(transport.materialize(),port)
    path=root/'synthesized.emuir.sqlite';write_compact_emuir(path,ir.to_dict())
    return case,split,fpga,transport,logical,ir,path


@pytest.mark.parametrize('shared',[False,True])
@pytest.mark.parametrize('fpga_index',[0,1,2,3])
def test_native_whole_lowering_matches_all_logical_records_and_boundary_identities(owners,tmp_path,shared,fpga_index):
    case,split,fpga,transport,logical,ir,path=prepare(owners,tmp_path,shared,fpga_index)
    expected=build_placement_ir(logical,transport.materialize(),ir)
    expected_path=tmp_path/'expected.emuir.sqlite';write_compact_emuir(expected_path,expected.to_dict())
    expected=EmuIR.load(expected_path)
    identities=build_boundary_identity_database(transport.materialize(),expected,ir)
    output=tmp_path/'native/placement.emuir.sqlite'
    with patch('emuflow.lowering.build_placement_ir',side_effect=AssertionError('Python graph merge')), \
         patch('emuflow.lowering.EmuIR.load',side_effect=AssertionError('Python graph import')), \
         patch('emuflow.boundary_timing.iter_boundary_identity_records',side_effect=AssertionError('Python endpoint binder')):
        report=run_placement_ir_lowering_from_split(split,fpga,transport.manifest_path,path,output)
    assert report['implementation_id']=='native-placement-lowering-v1'
    assert EmuIR.load(output).to_dict()==expected.to_dict()
    with read_boundary_identity_storage(output.with_name('boundary-identities.json')) as actual:
        assert {**actual,'endpoints':list(actual['endpoints'])}==identities
    assert report['original_instances']==len(logical['instances'])
    assert report['transport_instances']==len(ir.value['instances'])
    assert report['boundary_identity']['validation']['endpoints']==len(transport['endpoints'])
    assert not list(tmp_path.glob('.lowering-stage-*'))
    # Outputs are independent owners after the synthesized source is retired.
    path.unlink();assert EmuIR.load(output).to_dict()==expected.to_dict()


@pytest.mark.parametrize('damage',['source-generation','missing-shadow','multiple-top','incompatible-port','nonlocal-source','missing-external','double-shadow-driver'])
def test_native_lowering_rejects_broken_stage_contracts_without_publishing(owners,tmp_path,damage):
    from emuflow.errors import ValidationError
    from emuflow.placement_lowering_native import NativeToolProcess
    case,split,fpga,transport,logical,ir,path=prepare(owners,tmp_path,damage=='incompatible-port')
    if damage=='missing-shadow':
        if not transport['shadow_signals']:pytest.skip('requires RX')
        ir.value['nets']=[n for n in ir.value['nets'] if not n['id'].startswith('shadow')]
    elif damage=='multiple-top':
        if not transport['source_signals']:pytest.skip('requires TX')
        repeated=deepcopy(next(n for n in ir.value['nets'] if n['id'].startswith('source')))
        repeated['id']+='duplicate';ir.value['nets'].append(repeated)
    elif damage=='incompatible-port':
        next(p for p in ir.value['ports'] if p['id']=='shared_reset')['width']+=1
    elif damage=='missing-external':
        if not transport['endpoints']:pytest.skip('requires a boundary endpoint')
        ir.value['nets']=[n for n in ir.value['nets'] if not n['id'].startswith(('tx_','rx_'))]
    elif damage=='double-shadow-driver':
        if not transport['shadow_signals']:pytest.skip('requires RX')
        cell=deepcopy(next(c for c in ir.value['instances'] if c['id'].startswith('shadow')))
        cell['id']='duplicate-shadow-driver';ir.value['instances'].append(cell)
        next(n for n in ir.value['nets'] if n['id'].startswith('shadow'))['drivers'].append({'instance':cell['id'],'port':'Q','bit':0})
    elif damage=='nonlocal-source':
        with closing(sqlite3.connect(transport.store_path)) as db,db:
            count=db.execute("SELECT count(*) FROM signals WHERE fpga=? AND kind='source'",(fpga,)).fetchone()[0]
            if not count:pytest.skip('requires TX')
            db.execute("UPDATE signals SET signal='net:NOT_LOCAL' WHERE fpga=? AND kind='source' AND seq=(SELECT min(seq) FROM signals WHERE fpga=? AND kind='source')",(fpga,fpga))
    if damage not in ('source-generation','nonlocal-source'):write_compact_emuir(path,ir.value)
    output=tmp_path/'native/placement.emuir.sqlite'
    original=NativeToolProcess.__init__
    def change_before_launch(self,*args,**kwargs):
        path.chmod(path.stat().st_mode ^ 0o100)
        original(self,*args,**kwargs)
    with patch.object(NativeToolProcess,'__init__',change_before_launch if damage=='source-generation' else original):
        with pytest.raises(ValidationError):
            run_placement_ir_lowering_from_split(split,fpga,transport.manifest_path,path,output)
    assert not output.parent.exists() and not list(tmp_path.glob('.lowering-stage-*'))
