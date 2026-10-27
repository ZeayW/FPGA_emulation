"""Complete transport records, physical binding and engine-input equivalence."""
from contextlib import closing
from pathlib import Path
import sqlite3
import json
import shutil
import subprocess
import pytest

from emuflow.transport_storage import write_transport_storage
from emuflow.netlist import anchors_to_xdc_template
from emuflow.runtime import build_virtual_runtime,virtual_runtime_controller_to_systemverilog
from emuflow.virtual_board import build_virtual_board,virtual_binding_to_systemverilog,virtual_binding_to_xdc
from tests.transport_reference import write_transport_storage as reference,write_transport_systemverilog
from tests.test_split_overlay_native import owners
from tests.native_build import emuflow_native


def compare(expected,actual,platform,root,runtime=None):
    assert actual['summary']['scheduled_hops']==expected['summary']['scheduled_hops']
    assert actual['lane_map'].materialize()==expected['lane_map'].materialize()
    assert actual['lane_map'].canonical_sha256==expected['lane_map'].canonical_sha256
    for fpga in expected['transports']:
        assert actual['transports'][fpga].materialize()==expected['transports'][fpga].materialize()
        assert actual['anchors'][fpga].materialize()==expected['anchors'][fpga].materialize()
        old=root/(fpga+'.sv')
        write_transport_systemverilog(old,expected['transports'][fpga],platform)
        assert (root/'native'/fpga/'transport_schedule.sv').read_bytes()==old.read_bytes()
        assert (root/'native'/fpga/'virtual_anchors.xdc.template').read_text()==anchors_to_xdc_template(expected['anchors'][fpga])
    if expected['binding'] is not None:
        assert actual['binding'].materialize()==expected['binding'].materialize()
        assert actual['binding'].canonical_sha256==expected['binding'].canonical_sha256
        for fpga in expected['transports']:
            assert (root/'native'/fpga/'virtual_data_pins.xdc').read_text()==virtual_binding_to_xdc(expected['binding'],fpga)
            assert (root/'native'/fpga/'virtual_differential_io.sv').read_text()==virtual_binding_to_systemverilog(expected['binding'],fpga)
        actual['binding'].validate_transport_relations()
    if runtime is not None:
        assert (root/'native'/'virtual_runtime_controller.sv').read_text()==virtual_runtime_controller_to_systemverilog(runtime)
    with closing(sqlite3.connect(root/'native'/'transport.sqlite')) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'hops' in tables and not tables & {'endpoints','lanes','binding_entries','binding_adapter_ports'}
        assert 'record_json' not in {row[1] for row in db.execute('PRAGMA table_info(hops)')}
        assert db.execute('SELECT count(*) FROM hops').fetchone()[0]==actual['summary']['scheduled_hops']
        assert db.execute('SELECT count(*) FROM endpoints').fetchone()[0]==2*actual['summary']['scheduled_hops']


def test_native_hop_owner_matches_every_record_and_streamed_rtl(owners,tmp_path):
    case,source,assignment,schedule=owners
    kwargs={'design':case.ir.value['design'],'clocks':case.ir.value['clocks']}
    expected=reference(tmp_path/'reference',assignment,schedule,case.platform,**kwargs)
    actual=write_transport_storage(tmp_path/'native',assignment,schedule,case.platform,emit_rtl=True,**kwargs)
    compare(expected,actual,case.platform,tmp_path)


def test_native_transport_joins_the_original_chimew_lane_owner(owners,tmp_path):
    from emuflow.pin_plan_storage import write_pin_plan_storage
    case,source,assignment,schedule=owners
    widths={link.id:link.transport_bits_per_cycle_per_direction for link in case.platform.links}
    budgets={(r['link'],r['from'],r['to']):r for r in schedule.get('bank_allocations',[])}
    def next_lane(entry):
        budget=budgets.get((entry['link'],entry['from'],entry['to']))
        if budget is None:return (entry['lane']+1)%widths[entry['link']]
        return budget['lane_offset']+(entry['lane']-budget['lane_offset']+1)%budget['lanes']
    pin_plan=write_pin_plan_storage(tmp_path/'pin-plan.json',{
        'schema':'emuflow.placement-aware-pin-plan/v1',
        'provider':'chimew-paper-plus-emuflow-electrical-slot-v1',
        'design':case.ir.value['design'],'platform':case.platform.name,
        'entries':[{'schedule_entry':entry['id'],'group':index,
                    'logical_lane':entry['lane'],'physical_lane':next_lane(entry)}
                   for index,entry in enumerate(schedule['entries'])],
    })
    kwargs={'design':case.ir.value['design'],'clocks':case.ir.value['clocks'],'pin_plan':pin_plan}
    expected=reference(tmp_path/'reference',assignment,schedule,case.platform,**kwargs)
    actual=write_transport_storage(tmp_path/'native',assignment,schedule,case.platform,emit_rtl=True,**kwargs)
    compare(expected,actual,case.platform,tmp_path)


@pytest.mark.parametrize('boundary',['producer','independent-audit'])
def test_transport_preserves_phase5_direction_banks_after_pin_remapping(owners,tmp_path,boundary):
    from emuflow.errors import ValidationError
    from emuflow.native_tools import native_file_generation
    from emuflow.pin_plan_storage import write_pin_plan_storage
    from emuflow.pin_planning import validate_bank_pin_assignment

    case,source,assignment,schedule=owners
    if not any(link.bank_count is not None for link in case.platform.links):
        pytest.skip('requires a banked link')
    entries=list(schedule['entries'])
    budgets={(r['link'],r['from'],r['to']):r for r in schedule['bank_allocations']}
    selected=entries[0];budget=budgets[selected['link'],selected['from'],selected['to']]
    link=next(link for link in case.platform.links if link.id==selected['link'])
    outside=(budget['lane_offset']+budget['lanes'])%link.transport_bits_per_cycle_per_direction
    assert not budget['lane_offset']<=outside<budget['lane_offset']+budget['lanes']
    remap={entry['id']:entry['lane'] for entry in entries}
    remap[selected['id']]=outside
    with pytest.raises(ValidationError,match='allocated direction banks'):
        validate_bank_pin_assignment(schedule,case.platform,remap)
    plan=write_pin_plan_storage(tmp_path/'pin-plan.json',{
        'schema':'emuflow.placement-aware-pin-plan/v1',
        'provider':'chimew-paper-plus-emuflow-electrical-slot-v1',
        'design':case.ir.value['design'],'platform':case.platform.name,
        'entries':[{'schedule_entry':entry['id'],'group':index,
                    'logical_lane':entry['lane'],
                    'physical_lane':remap[entry['id']] if boundary=='producer' else entry['lane']}
                   for index,entry in enumerate(entries)],
    })
    output=tmp_path/'native'
    kwargs={'design':case.ir.value['design'],'clocks':case.ir.value['clocks'],'pin_plan':plan}
    if boundary=='producer':
        with pytest.raises(ValidationError,match='allocated direction banks'):
            write_transport_storage(output,assignment,schedule,case.platform,**kwargs)
        assert not (output/'transport.sqlite').exists()
    else:
        write_transport_storage(output,assignment,schedule,case.platform,**kwargs)
        # Matching two corrupted representations must not bypass the direction
        # policy retained in the original schedule owner.
        with closing(sqlite3.connect(plan.store_path)) as db,db:
            db.execute('UPDATE entries SET physical_lane=? WHERE schedule_entry=?',
                       (outside,selected['id']))
        changed=tmp_path/'changed-banks.sqlite'
        shutil.copyfile(output/'transport.sqlite',changed)
        with closing(sqlite3.connect(changed)) as db,db:
            db.execute('UPDATE hops SET lane=? WHERE entry_id=?',(outside,selected['id']))
            configuration=json.loads(db.execute("SELECT value FROM metadata WHERE key='configuration'").fetchone()[0])
            configuration['sources'][2]['generation']=list(native_file_generation(plan.store_path))
            db.execute("UPDATE metadata SET value=? WHERE key='configuration'",(json.dumps(configuration),))
        result=subprocess.run([str(emuflow_native()),'phase6','audit-transport',str(changed),str(tmp_path/'rejected-banks.json')],capture_output=True,text=True)
        assert result.returncode!=0 and 'allocated direction banks' in result.stderr+result.stdout
        assert not (tmp_path/'rejected-banks.json').exists()


from tests.test_phase6_virtual_board import inputs as board_inputs

def test_native_fixed_pin_binding_and_all_engine_inputs_match(board_inputs,tmp_path):
    from emuflow.ir import EmuIR
    from emuflow.platform import Platform
    from emuflow.phase3_assignment_index import write_phase3_assignment_index
    from tests.schedule_store_fixture import write_tdm_schedule_storage
    from emuflow.tdm_schedule_storage import read_tdm_schedule
    value,board,assignment,schedule=board_inputs
    platform=Platform.from_dict(board)
    path=tmp_path/'assignment.sqlite';write_phase3_assignment_index(path,EmuIR(value),assignment)
    write_tdm_schedule_storage(tmp_path/'schedule.json',schedule)
    current=read_tdm_schedule(tmp_path/'schedule.json')
    runtime=build_virtual_runtime(current,platform,{'clk':1.0})
    kwargs={'design':value['design'],'clocks':value['clocks'],'virtual_board':build_virtual_board(platform)}
    expected=reference(tmp_path/'reference',path,current,platform,**kwargs)
    actual=write_transport_storage(tmp_path/'native',path,current,platform,runtime=runtime,emit_rtl=True,**kwargs)
    compare(expected,actual,platform,tmp_path,runtime)
    # The compatibility binding view follows this compact record. Altering
    # both apparent representations must still fail against original wiring.
    changed=tmp_path/'changed-pins.sqlite'
    shutil.copyfile(tmp_path/'native'/'transport.sqlite',changed)
    with closing(sqlite3.connect(changed)) as db:
        row=db.execute('SELECT fpga,p_port,record_json FROM binding_physical_ports LIMIT 1').fetchone()
        record=json.loads(row[2]);record['p']['package_pin']='FORGED_PIN'
        db.execute('UPDATE binding_physical_ports SET record_json=? WHERE fpga=? AND p_port=?',
                   (json.dumps(record),row[0],row[1]));db.commit()
    result=subprocess.run([str(emuflow_native()),'phase6','audit-transport',str(changed),str(tmp_path/'rejected-pins.json')],capture_output=True,text=True)
    assert result.returncode!=0 and 'original fixed wiring' in result.stderr+result.stdout
    assert not (tmp_path/'rejected-pins.json').exists()


@pytest.mark.parametrize('damage',['slot','missing-hop','source-generation'])
def test_native_audit_rejects_changed_original_hop_facts(owners,tmp_path,damage):
    case,source,assignment,schedule=owners
    write_transport_storage(tmp_path/'native',assignment,schedule,case.platform,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'])
    path=tmp_path/'changed.sqlite';shutil.copyfile(tmp_path/'native'/'transport.sqlite',path)
    with closing(sqlite3.connect(path)) as db:
        if damage=='slot':db.execute('UPDATE hops SET arrival_slot=arrival_slot+1 WHERE entry_id=(SELECT min(entry_id) FROM hops)')
        elif damage=='missing-hop':db.execute('DELETE FROM hops WHERE entry_id=(SELECT min(entry_id) FROM hops)')
        else:
            configuration=json.loads(db.execute("SELECT value FROM metadata WHERE key='configuration'").fetchone()[0])
            configuration['sources'][0]['generation'][2]+=1
            db.execute("UPDATE metadata SET value=? WHERE key='configuration'",(json.dumps(configuration),))
        db.commit()
    output=tmp_path/'rejected.json'
    result=subprocess.run([str(emuflow_native()),'phase6','audit-transport',str(path),str(output)],capture_output=True,text=True)
    assert result.returncode!=0 and '"status":"fail"' in result.stderr+result.stdout
    assert not output.exists()
