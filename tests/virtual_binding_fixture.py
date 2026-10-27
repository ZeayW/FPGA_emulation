"""Native transport from explicit small schedule/physical-lane facts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import weakref

from emuflow.ir.ir import EmuIR
from emuflow.transport.pin_plan_storage import write_pin_plan_storage
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.transport.transport_storage import write_transport_storage
from emuflow.transport.virtual_board import write_virtual_binding_xdc, write_virtual_binding_systemverilog
from tests.phase3_store_fixture import write_assignment_index_fixture
from tests.schedule_store_fixture import write_tdm_schedule_storage


def native_binding(platform, board, schedule, artifacts):
    assert len(schedule['entries']) <= 4096
    temporary = tempfile.TemporaryDirectory(prefix='virtual-binding-facts-')
    root = Path(temporary.name)
    design = dict(name=schedule['design'], top=schedule['design'], source_format='fixture')
    ir = EmuIR(dict(schema='emuflow.emuir/v1', design=design,
                    ports=[], instances=[], nets=[], clocks=[]))
    assignment = dict(design=schedule['design'], platform=platform.name,
        instance_assignment={}, cut_nets=[], partitions=[dict(fpga=f.id) for f in platform.fpgas])
    write_assignment_index_fixture(root/'assignment.sqlite', ir, assignment)
    value = deepcopy(schedule)
    value.update(schema='emuflow.tdm-schedule/v1', metrics={'frame_slots':max(
        [32]+[e['arrival_slot']+1 for e in value['entries']]),
        'completion_slot':max([0]+[e['arrival_slot'] for e in value['entries']])})
    value.setdefault('domain_schedules', [])
    value.setdefault('demand_completions', [])
    value.setdefault('routes', [])
    for i, entry in enumerate(value['entries']):
        entry.setdefault('demand', entry['id'])
        entry.setdefault('hop', 0)
        entry.setdefault('capacity_key', 'link:'+entry['link'])
        entry.setdefault('ready_slot', 0)
        if not any(r['id']==entry['demand'] for r in value['routes']):
            value['routes'].append(dict(id=entry['demand'],net=entry['net'],
                source=entry['from'],sinks=[entry['to']],tree_edges=[dict(
                    link=entry['link'],**{'from':entry['from'],'to':entry['to']})]))
    write_tdm_schedule_storage(root/'schedule.json', value)
    typed = read_tdm_schedule(root/'schedule.json')
    lanes = {row['schedule_entry']:row['lane'] for row in artifacts.get('lane_map',{}).get('entries', [])}
    plan = None
    if lanes:
        plan = write_pin_plan_storage(root/'plan.json',dict(schema='emuflow.placement-aware-pin-plan/v1',
            provider='chimew-paper-plus-emuflow-electrical-slot-v1',design=design,platform=platform.name,
            entries=[dict(schedule_entry=e['id'],group=i,logical_lane=e['lane'],physical_lane=lanes[e['id']])
                     for i,e in enumerate(value['entries'])]))
    try:
        output = write_transport_storage(root/'native', root/'assignment.sqlite', typed, platform,
            design=design,clocks=[],virtual_board=board,pin_plan=plan,emit_rtl=True)
    except BaseException:
        typed.close();temporary.cleanup();raise
    schedule.clear();schedule.update(value)
    artifacts.clear();artifacts.update(lane_map=output['lane_map'],transports=output['transports'])
    binding = output['binding']
    binding._fixture_inputs = (output, typed, plan)
    binding._fixture_cleanup = weakref.finalize(binding, temporary.cleanup)
    return binding


def binding_text(binding, fpga, *, systemverilog=False):
    with tempfile.TemporaryDirectory(prefix='binding-text-fixture-') as temporary:
        path=Path(temporary)/'adapter'
        writer=write_virtual_binding_systemverilog if systemverilog else write_virtual_binding_xdc
        writer(path,binding,fpga)
        return path.read_text()
