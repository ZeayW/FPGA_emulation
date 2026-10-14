"""Phase 6 materializes and independently replays the declared virtual board."""
import copy
from pathlib import Path

import pytest

from emuflow.default_platform import research_platform_path
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.partition import assign_clusters, build_clusters, normalize_partition_constraints
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from emuflow.tdm import build_tdm_schedule
from emuflow.timing_routing import route_system_native
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def inputs():
    ir=import_yosys_json(ROOT/'examples/yosys/counter.json',top='counter',clocks=['clk'])
    platform=Platform.load(research_platform_path(2))
    constraints=normalize_partition_constraints(None,ir,platform)
    clusters=build_clusters(ir,constraints)
    assignment=assign_clusters(ir,platform,clusters,constraints,seed=20260727)
    routes=route_system_native(assignment,platform,normalize_route_constraints(None,platform,frame_slots=32),executable=str(tlr_router()))
    schedule=build_tdm_schedule(routes,platform)
    return ir.value,platform.to_dict(),assignment,schedule


def emit(tmp_path, inputs, *, enabled=True, managed=False):
    ir,platform,assignment,schedule=copy.deepcopy(inputs)
    if not enabled:platform['platform'].pop('virtual_board_model')
    for name,value in [('ir',ir),('platform',platform),('assignment',assignment),('schedule',schedule)]:
        write_json(tmp_path/(name+'.json'),value)
    args=[tmp_path/'ir.json',tmp_path/'assignment.json',tmp_path/'schedule.json',tmp_path/'platform.json']
    out=tmp_path/'split'
    report=run_phase6(*args,out,equivalence_cycles=2,managed_storage=managed)
    return args,out,report


@pytest.mark.parametrize('managed',[False,True])
def test_phase6_automatically_emits_binding_and_replay_matches_report(tmp_path,inputs,managed):
    args,out,report=emit(tmp_path,inputs,managed=managed)
    validation=validate_phase6(*args,out/'manifest.json')
    assert validation==report['validation']
    assert report['virtual_board_binding']['physical_backend_status']=='not_consumed_by_ooc_backend'
    board=read_json(out/'virtual_board_wiring.json')
    binding=read_json(out/'virtual_board_binding.json')
    assert board['metrics']['differential_pairs']==360
    assert board['metrics']['physical_conductors']==720
    assert board['metrics']['endpoint_data_pins']==1440
    assert len(binding['entries'])==len(inputs[3]['entries'])
    assert binding['entries'], 'The integration fixture must exercise cross-FPGA I/O'
    assert (out/'fpga0/virtual_data_pins.xdc').exists()
    assert (out/'fpga1/virtual_data_pins.xdc').exists()
    manifest=read_json(out/'manifest.json')
    assert manifest['virtual_board_binding']['differential_io']=={
        fpga:f'{fpga}/virtual_differential_io.sv' for fpga in ('fpga0','fpga1')
    }
    for fpga in ('fpga0','fpga1'):
        adapter=(out/fpga/'virtual_differential_io.sv').read_text()
        assert f'module emuflow_virtual_io_{fpga}' in adapter
        xdc=(out/fpga/'virtual_data_pins.xdc').read_text()
        for entry in binding['entries']:
            for role,primitive in [('source','OBUFDS'),('sink','IBUFDS')]:
                endpoint=entry[role]
                if endpoint['fpga']==fpga:
                    assert primitive in adapter
                    for polarity in ('p','n'):
                        pin=endpoint[polarity]
                        assert pin['port'] in adapter
                        assert f"set_property PACKAGE_PIN {pin['package_pin']} [get_ports {{{pin['port']}}}]" in xdc
                        assert f"set_property IOSTANDARD LVDS [get_ports {{{pin['port']}}}]" in xdc
    assert read_json(out/'lane_map.json')['binding_status']=='logical_only'


@pytest.mark.parametrize('mutation',['manifest','missing','wiring','polarity','binding','xdc',
                                     'adapter','missing-adapter','missing-adapter-manifest'])
def test_required_virtual_pin_artifacts_cannot_be_removed_or_modified(tmp_path,inputs,mutation):
    args,out,_=emit(tmp_path,inputs)
    if mutation=='manifest':
        m=read_json(out/'manifest.json');m.pop('virtual_board_binding');write_json(out/'manifest.json',m)
    elif mutation=='missing':
        (out/'virtual_board_binding.json').unlink()
    elif mutation=='wiring':
        b=read_json(out/'virtual_board_wiring.json');b['wires'][0]['endpoints'][0]['p']['package_pin']='INVALID';write_json(out/'virtual_board_wiring.json',b)
    elif mutation=='polarity':
        b=read_json(out/'virtual_board_wiring.json')
        endpoint=b['wires'][0]['endpoints'][0]
        endpoint['p'],endpoint['n']=endpoint['n'],endpoint['p']
        write_json(out/'virtual_board_wiring.json',b)
    elif mutation=='binding':
        b=read_json(out/'virtual_board_binding.json');b['board_sha256']='0'*64;write_json(out/'virtual_board_binding.json',b)
    elif mutation=='xdc':
        with (out/'fpga0/virtual_data_pins.xdc').open('a') as f:f.write('# modified\n')
    elif mutation=='adapter':
        with (out/'fpga0/virtual_differential_io.sv').open('a') as f:f.write('// modified\n')
    elif mutation=='missing-adapter':
        (out/'fpga0/virtual_differential_io.sv').unlink()
    else:
        m=read_json(out/'manifest.json')
        m['virtual_board_binding'].pop('differential_io')
        write_json(out/'manifest.json',m)
    with pytest.raises(ValidationError):validate_phase6(*args,out/'manifest.json')


def test_legacy_platform_without_model_keeps_board_independent_outputs(tmp_path,inputs):
    args,out,report=emit(tmp_path,inputs,enabled=False)
    assert 'virtual_board_binding' not in report
    assert not (out/'virtual_board_wiring.json').exists()
    assert validate_phase6(*args,out/'manifest.json')==report['validation']
