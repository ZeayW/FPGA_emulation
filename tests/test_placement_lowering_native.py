"""Native lowering ownership, source preservation, connectivity and identities."""
from tests.phase3_store_fixture import write_assignment_index_fixture
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.boundary_timing import validate_boundary_identity_database
from emuflow.boundary_identity_storage import read_boundary_identity_storage
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.lowering import run_placement_ir_lowering_from_split
from emuflow.netlist import transport_endpoint_port
from tests.native_split_fixture import materialize_partition_fixture
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
        ports.append({'id':port,'name':port,'width':max(1,len(signals)),'direction':direction,'clock':False,'reset':False})
        if not signals:
            top={'instance':None,'port':port,'bit':0}
            nets.append({'id':'dummy-'+kind,'name':'dummy-'+kind,'cut_class':'combinational',
                         'drivers':[top] if kind=='source' else [],
                         'sinks':[top] if kind=='shadow' else [],'fanout':int(kind=='shadow')})
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
    for net in nets:
        net['attributes'] = {'opaque': ['雪', 7, True]}
        net.setdefault('aliases', ['alias-' + net['id']])
    return EmuIR({'schema':'emuflow.emuir/v1','design':{'name':'transport','top':'transport','source_format':'test'},
                  'ports':ports,'instances':cells,'nets':nets,'clocks':[],'warnings':['preserve warning']})


def prepare(owners,root,shared=False,fpga_index=0):
    case,source,assignment,schedule=owners
    if fpga_index>=len(case.platform.fpgas):pytest.skip('partition is absent')
    if shared:
        value=deepcopy(case.ir.value)
        for net in value['nets']:
            net['attributes'] = {'opaque_dut': ['保留', 11]}
            net['aliases'] = ['dut-alias-' + net['id']]
        value['ports'].append({'id':'shared_reset','name':'shared_reset','direction':'input','width':1,'clock':False,'reset':True})
        value['nets'].append({'id':'shared_reset','name':'shared_reset','cut_class':'reset',
            'drivers':[{'instance':None,'port':'shared_reset','bit':0}],
            'sinks':[{'instance':c['id'],'port':'R','bit':0} for c in value['instances'] if c['type']=='FDRE']})
        value['nets'][-1]['fanout']=len(value['nets'][-1]['sinks'])
        case.ir=EmuIR(value);write_compact_emuir(source,value)
        assigned=deepcopy(case._phase5_assignment)
        declared={row['fpga'] for row in assigned['partitions']}
        assigned['partitions'].extend({'fpga':f.id,'resources':{}} for f in case.platform.fpgas if f.id not in declared)
        write_assignment_index_fixture(assignment,case.ir,assigned)
    split=root/'split.sqlite'
    build_native_split(split,source,assignment,schedule,expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    artifacts=write_transport_storage(root/'transport',assignment,schedule,case.platform,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'])
    fpga=case.platform.fpgas[fpga_index].id;transport=artifacts['transports'][fpga]
    logical=materialize_partition_fixture(split,fpga)
    port=next((p for p in logical['ports'] if p['id']=='shared_reset'),None) if shared else None
    if shared and port is None:pytest.skip('partition has no shared DUT input')
    ir=synthesized_transport(transport.materialize(),port)
    path=root/'synthesized.emuir.sqlite';write_compact_emuir(path,ir.to_dict())
    return case,split,fpga,transport,logical,ir,path


@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('fpga_index', [0, 1, 2, 3])
def test_native_whole_lowering_preserves_sources_and_boundary_identities(owners, tmp_path, shared, fpga_index):
    case, split, fpga, transport, logical, ir, path = prepare(owners, tmp_path, shared, fpga_index)
    output = tmp_path / 'native/placement.emuir.sqlite'
    import emuflow.boundary_timing as boundary
    assert not hasattr(boundary, 'iter_boundary_identity_records')
    with patch('emuflow.ir.EmuIR.load', side_effect=AssertionError('Python graph import')):
        report = run_placement_ir_lowering_from_split(split, fpga, transport.manifest_path, path, output)
    assert report['implementation_id'] == 'native-placement-lowering-v1'
    actual = EmuIR.load(output)
    before = actual.to_dict()
    cells = {cell['id']: cell for cell in actual.value['instances']}
    # Preserve every DUT and synthesized primitive field, including parameters,
    # constants and opaque attributes; only transport identities are namespaced.
    assert cells == {
        **{cell['id']: cell for cell in logical['instances']},
        **{'__emuflow_transport__/' + cell['id']: {
            **cell, 'id': '__emuflow_transport__/' + cell['id'],
            'name': '__emuflow_transport__/' + cell['id'],
        } for cell in ir.value['instances']},
    }
    nets = {net['id']: net for net in actual.value['nets']}
    logical_ids = {net['original_net'] for net in logical['nets']}
    for net in logical['nets']:
        merged = nets[net['original_net']]
        assert merged['name'] == net['name']
        for key, value in net.items():
            if key not in ('id', 'drivers', 'sinks', 'fanout'):
                assert merged[key] == value
        for role in ('drivers', 'sinks'):
            for endpoint in net[role]:
                if not (endpoint['instance'] or '').startswith('__emuflow_rx_'):
                    assert endpoint in merged[role]
    consumed = {'dummy-source', 'dummy-shadow', 'shared-clock'}
    for signal in transport['source_signals']:
        consumed.add('source' + str(signal['index']))
        net_id = signal['signal'].removeprefix('net:')
        assert {'instance': '__emuflow_transport__/source' + str(signal['index']),
                'port': 'I0', 'bit': 0, 'tag': signal['index']} in nets[net_id]['sinks']
    shadow_nets = {endpoint['signal']: endpoint['net'] for endpoint in transport['endpoints'] if endpoint['kind']=='rx'}
    for signal in transport['shadow_signals']:
        name = 'shadow' + str(signal['index'])
        cell_id = '__emuflow_transport__/' + name
        endpoint = {'instance': cell_id, 'port': 'Q', 'bit': 0, 'tag': signal['index']}
        target = shadow_nets[signal['signal']]
        if target in logical_ids:
            consumed.add(name)
        else:
            target = '__emuflow_transport__/' + name
        assert endpoint in nets[target]['drivers']
        assert sum(ep['instance'] == cell_id for net in nets.values() for ep in net['drivers']) == 1
    remaining = [net for net in ir.value['nets'] if net['id'] not in consumed]
    assert set(nets) == logical_ids | {'__emuflow_transport__/' + net['id'] for net in remaining}
    for net in remaining:
        expected = {**net, 'id': '__emuflow_transport__/' + net['id'],
                    'name': '__emuflow_transport__/' + net['name']}
        for role in ('drivers', 'sinks'):
            expected[role] = [dict(ep, instance='__emuflow_transport__/' + ep['instance'])
                             if ep['instance'] is not None else dict(ep) for ep in net[role]
                             if ep['instance'] is not None or ep['port'] not in ('source_values', 'shadow_values')]
        expected['fanout'] = len(expected['sinks'])
        expected.setdefault('aliases', [])
        assert nets[expected['id']] == expected
    if shared:
        candidates = [net for net in nets.values() if any(ep['instance'] is None and ep['port']=='shared_reset'
                      for ep in net['drivers'])]
        assert len(candidates) == 1 and candidates[0]['id'] == 'shared_reset'
        assert sum(ep['instance'] is None and ep['port']=='shared_reset' for ep in candidates[0]['drivers']) == 1
        for index in range(3):
            assert {'instance':'__emuflow_transport__/clock_sink'+str(index),'port':'C','bit':0,'tag':index} in candidates[0]['sinks']
    assert not any(net.startswith('__emuflow_transport__/dummy-') for net in nets)
    assert not {'source_values', 'shadow_values'} & {port['id'] for port in actual.value['ports']}
    assert not any((ep['instance'] or '').startswith('__emuflow_rx_')
                   for net in nets.values() for ep in net['drivers'] + net['sinks'])
    # Compare the complete native boundary population to independent facts from
    # this synthesized fixture. No second transport-to-EmuIR binder is retained.
    shadow_index = {row['signal']: row['index'] for row in transport['shadow_signals']}
    with read_boundary_identity_storage(output.with_name('boundary-identities.json')) as emitted:
        emitted_by_id = {row['id']: row for row in emitted['endpoints']}
        assert set(emitted_by_id) == {row['id'] for row in transport['endpoints']}
        for source in transport['endpoints']:
            port, lane = transport_endpoint_port(source), source['lane']
            is_dut = source['kind'] == 'tx' and source['signal'].startswith('net:')
            registers = ([] if is_dut else [
                '__emuflow_transport__/shadow' + str(shadow_index[source['signal']])])
            expected_external = '__emuflow_transport__/' + port + ':' + str(lane)
            assert emitted_by_id[source['id']] == {
                **{key: source[key] for key in ('id', 'kind', 'schedule_entry', 'demand',
                    'link', 'peer', 'lane', 'logical_lane')},
                'logical_net': source['net'], 'internal_signal': source['signal'],
                'source_class': ('dut-net' if is_dut else 'captured-shadow'
                                 if source['kind'] == 'rx' else 'forwarded-shadow'),
                'merged_ir': {'external_port': port, 'external_port_bit': lane,
                    'external_net': expected_external,
                    'logical_net': source['net'] if source['net'] in nets else None,
                    'boundary_register_instances': registers},
            }
            external_role = 'sinks' if source['kind'] == 'tx' else 'drivers'
            assert {'instance': None, 'port': port, 'bit': lane} in nets[expected_external][external_role]
        validate_boundary_identity_database(emitted, transport)
    assert report['original_instances'] == len(logical['instances'])
    assert report['transport_instances'] == len(ir.value['instances'])
    assert report['boundary_identity']['validation']['endpoints'] == len(transport['endpoints'])
    assert not list(tmp_path.glob('.lowering-stage-*'))
    path.unlink()
    assert EmuIR.load(output).to_dict() == before


def test_removed_graph_lowering_interfaces_are_not_exposed():
    import emuflow.lowering as lowering
    from emuflow.cli import _build_parser
    assert not any(hasattr(lowering, name) for name in (
        'build_placement_ir', 'run_placement_ir_lowering',
        'run_placement_ir_lowering_from_value',
    ))
    parser = _build_parser()
    args = parser.parse_args(['lower-placement-ir', '--split', 'split.sqlite',
        '--fpga', 'fpga0', '--transport', 'transport.json',
        '--transport-ir', 'transport.sqlite', '--output', 'placement.sqlite'])
    assert args.split == Path('split.sqlite') and args.fpga == 'fpga0'
    with pytest.raises(SystemExit):
        parser.parse_args(['lower-placement-ir', '--netlist', 'netlist.json',
            '--transport', 'transport.json', '--transport-ir', 'transport.sqlite',
            '--output', 'placement.sqlite'])


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
