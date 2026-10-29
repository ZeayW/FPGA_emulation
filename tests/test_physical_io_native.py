"""Whole-board fixed-I/O publication, original authorities and fail-closed handoff."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import pytest
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation
from emuflow.physical.fixed_io import prepare_fixed_io
from emuflow.platform.default_platform import research_platform_path
from emuflow.platform.platform import Platform
from emuflow.transport.transport_storage import _RecordSequence
from emuflow.transport.virtual_board import build_virtual_board
from tests.native_build import emuflow_native
from tests.test_virtual_board import schedule_and_ports
from tests.virtual_binding_fixture import native_binding
from tests.virtual_io_reference import build_virtual_io_physical_contract


@pytest.fixture
def case(tmp_path):
    platform = Platform.load(research_platform_path(2))
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [('fpga0','fpga1',7,2),
        ('fpga0','fpga1',7,4), ('fpga1','fpga0',190,6)], forward_banks=9)
    binding = native_binding(platform, board, schedule, artifacts)
    root = binding.manifest_path.parent
    wiring = tmp_path/'wiring.json';wiring.write_text(json.dumps(board))
    typed_schedule = binding._fixture_inputs[1]
    args = [binding.manifest_path, artifacts['lane_map'].manifest_path, wiring, typed_schedule,
            {f.id: root/f.id/'virtual_data_pins.xdc' for f in platform.fpgas},
            {f.id: root/f.id/'virtual_differential_io.sv' for f in platform.fpgas}, tmp_path/'accepted']
    return binding, args


def test_complete_native_contract_matches_independent_member_replay(case, monkeypatch):
    binding,args = case
    expected = {fpga: build_virtual_io_physical_contract(binding.materialize(),fpga,
        xdc_path=args[4][fpga],adapter_path=args[5][fpga]) for fpga in args[4]}
    def no_hops(*unused):
        raise AssertionError('Python touched the hop population')
    monkeypatch.setattr(_RecordSequence, '__iter__', no_hops)
    outputs = prepare_fixed_io(*args)
    assert set(outputs) == set(args[4])
    for fpga,actual in outputs.items():
        reference = expected[fpga]
        assert actual['schema'] == 'emuflow.virtual-io-physical-contract/v2'
        for key in ('status','platform','design','fpga','metrics','xdc','adapter','implementation_contract'):
            assert actual[key] == reference[key]
        for a,b in zip(actual['entries'],reference['entries']):
            members = b.pop('schedule_entries')
            assert a.pop('schedule_entry_count') == len(members)
            assert a.pop('schedule_entries_sha256') == hashlib.sha256(
                b''.join((json.dumps(m,separators=(',',':'))+'\n').encode() for m in members)).hexdigest()
            assert a == b
    report = json.loads((args[-1]/'physical-io-report.json').read_text())
    assert report['implementation_id'] == 'native-physical-io-stage-v1'
    assert report['binding_validation']['scheduled_hops'] == 3
    assert not list(args[-1].parent.glob('.transport-stage-*'))
    assert not list(args[-1].parent.glob('.fixed-io-host-*'))


@pytest.mark.parametrize('damage', ['xdc','adapter','wiring','binding-header','lane-seal',
    'membership','pin','wire-id','bank-group','cable','standard','missing-wire','ddl',
    'selected-schedule','missing-adapter'])
def test_rejects_corrupted_or_resealed_sources_without_partial_output(case,damage):
    binding,args = case
    fpga = next(iter(args[4]))
    if damage in ('xdc','adapter'):
        path = args[4 if damage=='xdc' else 5][fpga]
        path.write_text(path.read_text()+'\n// changed contract\n')
    elif damage == 'wiring':
        value=json.loads(args[2].read_text());value['wires'][0]['cable']='different';args[2].write_text(json.dumps(value))
    elif damage in ('binding-header','lane-seal'):
        path=args[0 if damage=='binding-header' else 1]
        value=json.loads(path.read_text())
        if damage=='binding-header':value['header']['board_sha256']='0'*64
        else:value['sections']['entries']['sha256']='0'*64
        path.write_text(json.dumps(value))
    elif damage == 'selected-schedule':
        new=args[-1].parent/'other-schedule.sqlite';shutil.copyfile(args[3].store_path,new)
        class Other:
            store_path=new
        args[3]=Other()
    elif damage == 'missing-adapter':
        args[5].pop(fpga)
    else:
        with closing(sqlite3.connect(binding.store_path)) as db,db:
            if damage=='membership':db.execute('UPDATE binding_physical_ports SET schedule_entry_count=schedule_entry_count+1')
            elif damage=='pin':db.execute("UPDATE binding_physical_ports SET record_json=json_set(record_json,'$.p.package_pin','INVENTED')")
            elif damage=='wire-id':db.execute("UPDATE wire_bindings SET wire_id=wire_id||'_different'")
            elif damage=='bank-group':db.execute("UPDATE wire_bindings SET bank_group='null'")
            elif damage=='cable':db.execute("UPDATE wire_bindings SET cable='null'")
            elif damage=='standard':db.execute("UPDATE metadata SET value='\"INVALID\"' WHERE key='iostandard'")
            elif damage=='missing-wire':db.execute('DELETE FROM wire_bindings WHERE wire=0')
            else:
                db.execute('DROP VIEW lanes');db.execute('CREATE VIEW lanes AS SELECT * FROM hops')
        for path in args[:2]:
            value=json.loads(path.read_text());value['store_bytes']=binding.store_path.stat().st_size
            value['store_sha256']=hashlib.sha256(binding.store_path.read_bytes()).hexdigest()
            path.write_text(json.dumps(value))
    with pytest.raises((ValidationError,ValueError)):
        prepare_fixed_io(*args)
    assert not args[-1].exists()
    assert not list(args[-1].parent.glob('.transport-stage-*'))


def request_from_args(binding,args):
    rows=[('transport',binding.store_path,'transport-store-v2'),
          ('binding',args[0],'physical-input-document-v1'),('lane_map',args[1],'physical-input-document-v1'),
          ('wiring',args[2],'physical-input-document-v1'),('schedule',args[3].store_path,'tdm-schedule-store-v4')]
    rows += [('xdc.'+f,p,'exact-sha256-text-v1') for f,p in args[4].items()]
    rows += [('adapter.'+f,p,'exact-sha256-text-v1') for f,p in args[5].items()]
    return {'schema':'emuflow.physical-io-stage/v1','sources':[{'role':role,'path':str(path.resolve()),
        'schema':schema,'generation':list(native_file_generation(path))} for role,path,schema in rows]}


@pytest.mark.parametrize('damage', ['source-generation','unknown-role','duplicate-role','version','unknown-field','relative-output','existing-output'])
def test_native_request_and_publication_admission(case,damage):
    binding,args=case
    request=request_from_args(binding,args)
    if damage=='source-generation':request['sources'][1]['generation'][2]+=1
    elif damage=='unknown-role':request['sources'][1]['role']='unknown'
    elif damage=='duplicate-role':request['sources'].append(request['sources'][0])
    elif damage=='version':request['schema']='emuflow.physical-io-stage/v2'
    elif damage=='unknown-field':request['ignored']=True
    elif damage=='existing-output':
        args[-1].mkdir();(args[-1]/'accepted').write_text('original generation')
    path=args[-1].parent/'request.json';path.write_text(json.dumps(request))
    output=Path('relative-generation') if damage=='relative-output' else args[-1]
    result=subprocess.run([str(emuflow_native()),'physical','prepare-fixed-io',str(path),str(output)],capture_output=True,text=True,cwd=args[-1].parent)
    assert result.returncode!=0,result.stdout
    if damage=='existing-output':assert {p.name:p.read_text() for p in args[-1].iterdir()}=={'accepted':'original generation'}
    else:assert not args[-1].exists()
    assert not (args[-1].parent/'relative-generation').exists()


def test_empty_schedule_keeps_both_fixed_io_partition_contracts(tmp_path):
    platform=Platform.load(research_platform_path(2));board=build_virtual_board(platform)
    schedule,artifacts=schedule_and_ports(platform,[])
    binding=native_binding(platform,board,schedule,artifacts);root=binding.manifest_path.parent
    wiring=tmp_path/'wiring.json';wiring.write_text(json.dumps(board))
    result=prepare_fixed_io(binding.manifest_path,artifacts['lane_map'].manifest_path,wiring,
        binding._fixture_inputs[1],{f.id:root/f.id/'virtual_data_pins.xdc' for f in platform.fpgas},
        {f.id:root/f.id/'virtual_differential_io.sv' for f in platform.fpgas},tmp_path/'empty')
    for value in result.values():
        assert value['entries']==[]
        assert value['metrics']=={'differential_pairs':0,'physical_ports':0,'scheduled_hops':0}


def test_replaced_python_contract_builder_is_absent():
    import emuflow.transport.virtual_board as module
    assert not hasattr(module,'build_virtual_io_physical_contract')


@pytest.mark.parametrize('damage', ['contract','source'])
def test_adoption_rejects_a_generation_changed_while_reading_contracts(case,monkeypatch,damage):
    import emuflow.physical.fixed_io as host
    _,args=case
    read=host._small
    changed=[]
    def mutate(path,*bounds):
        value=read(path,*bounds)
        if Path(path).name=='virtual-io-physical-contract.json' and not changed:
            changed.append(True)
            target=Path(path) if damage=='contract' else next(iter(args[4].values()))
            target.write_bytes(target.read_bytes()+b'\n')
        return value
    monkeypatch.setattr(host,'_small',mutate)
    with pytest.raises(ValidationError,match='generation changed'):
        prepare_fixed_io(*args)
    assert changed
