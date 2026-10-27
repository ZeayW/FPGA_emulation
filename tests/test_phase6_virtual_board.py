from tests.phase3_store_fixture import write_current_assignment_fixture
from tests.native_route_fixture import route_system_native
"""Phase 6 materializes and independently replays the declared virtual board."""
from contextlib import closing
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import emuflow.virtual_board as virtual_board_module
from tests.virtual_binding_fixture import binding_text
from emuflow.default_platform import research_platform_path
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.emuir_compact import write_compact_emuir
from emuflow.io import read_json, write_json
from tests.native_partition_facts import (build_clusters, normalize_partition_constraints)
from tests.partition_reference import assign_clusters
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from tests.schedule_reference import build_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage
from emuflow.transport_storage import (
    VIRTUAL_BINDING_STORAGE_SCHEMA,
    read_virtual_binding,
)
from emuflow.virtual_board import (
    IO_CONTRACT_STORAGE_SCHEMA,
    build_virtual_io_physical_contract,
)
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def inputs():
    ir=import_yosys_json(ROOT/'examples/yosys/counter.json',top='counter',clocks=['clk'])
    platform=Platform.load(research_platform_path(2))
    constraints=normalize_partition_constraints(None,ir,platform)
    clusters=build_clusters(ir,constraints, cut_mode="sequential-only")
    assignment=assign_clusters(ir,platform,clusters,constraints,seed=20260727)
    routes=route_system_native(assignment,platform,normalize_route_constraints(None,platform,frame_slots=32),executable=str(tlr_router()))
    schedule=build_tdm_schedule(routes,platform)
    # Keep this independent registered-transport oracle while publishing its
    # completion facts in the current typed schema.
    routes_by_id={route['id']:route for route in schedule['routes']}
    for route in schedule['routes']:
        route.pop('transport_round',None)
    for completion in schedule['demand_completions']:
        completion.pop('transport_round',None)
        sinks=routes_by_id[completion['demand']]['sinks']
        completion['sink_arrival_slots']={
            entry['to']:entry['arrival_slot'] for entry in schedule['entries']
            if entry['demand']==completion['demand'] and entry['to'] in sinks
        }
    return ir.value,platform.to_dict(),assignment,schedule


def emit(tmp_path, inputs, *, enabled=True, managed=False):
    ir,platform,assignment,schedule=copy.deepcopy(inputs)
    if not enabled:
        platform['platform'].pop('virtual_board_model')
        platform['platform'].pop('virtual_board_wiring')
    for name,value in [('ir',ir),('platform',platform)]:
        write_json(tmp_path/(name+'.json'),value)
    write_tdm_schedule_storage(tmp_path/'schedule.json',schedule)
    write_compact_emuir(tmp_path/'design.emuir.sqlite', ir)
    write_current_assignment_fixture(tmp_path/'assignment.json', EmuIR(ir), assignment)
    args=[tmp_path/'design.emuir.sqlite',tmp_path/'assignment.json',tmp_path/'schedule.json',tmp_path/'platform.json']
    out=tmp_path/'split'
    report=run_phase6(*args,out,equivalence_cycles=2,managed_storage=managed)
    return args,out,report


@pytest.mark.parametrize('managed',[False,True])
def test_phase6_automatically_emits_binding_and_replay_matches_report(tmp_path,inputs,managed):
    args,out,report=emit(tmp_path,inputs,managed=managed)
    validation=validate_phase6(*args,out/'manifest.json')
    assert validation==report['validation']
    assert report['virtual_board_binding']['physical_backend_status']=='phase7_consumption_required'
    board=read_json(out/'virtual_board_wiring.json')
    binding=read_json(out/'virtual_board_binding.json')
    raw_binding=json.loads(
        (out/'virtual_board_binding.json').read_text(encoding='utf-8')
    )
    assert raw_binding['schema']==VIRTUAL_BINDING_STORAGE_SCHEMA
    assert (out/'virtual_board_binding.json').stat().st_size < 4096
    manifest=read_json(out/'manifest.json')
    # Production native transport and independent replay have already passed;
    # verify the physical source facts directly rather than recreating a raw builder.
    assert report['validation']['virtual_board_binding']['status'] == 'pass'
    assert board['metrics']['differential_pairs']==360
    assert board['metrics']['physical_conductors']==720
    assert board['metrics']['endpoint_data_pins']==1440
    assert len(binding['entries'])==len(inputs[3]['entries'])
    assert binding['entries'], 'The integration fixture must exercise cross-FPGA I/O'
    assert (out/'fpga0/virtual_data_pins.xdc').exists()
    assert (out/'fpga1/virtual_data_pins.xdc').exists()
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


def test_indexed_binding_emits_adapter_without_iterating_hop_entries(tmp_path, inputs):
    _, out, _ = emit(tmp_path, inputs)
    view = read_virtual_binding(out / 'virtual_board_binding.json')
    materialized = view.materialize()

    class NoHopScan:
        def __iter__(self):
            raise AssertionError('adapter emission scanned hop-sized binding entries')

    view._sections['entries'] = NoHopScan()
    for fpga in ('fpga0', 'fpga1'):
        assert binding_text(view, fpga) == binding_text(materialized, fpga)
        assert binding_text(view, fpga, systemverilog=True) == binding_text(materialized, fpga, systemverilog=True)

        indexed_contract = build_virtual_io_physical_contract(
            view,
            fpga,
            xdc_path=out / fpga / 'virtual_data_pins.xdc',
            adapter_path=out / fpga / 'virtual_differential_io.sv',
        )
        legacy_contract = build_virtual_io_physical_contract(
            materialized,
            fpga,
            xdc_path=out / fpga / 'virtual_data_pins.xdc',
            adapter_path=out / fpga / 'virtual_differential_io.sv',
        )
        assert indexed_contract['schema'] == IO_CONTRACT_STORAGE_SCHEMA
        assert indexed_contract['metrics'] == legacy_contract['metrics']
        assert len(indexed_contract['entries']) == len(legacy_contract['entries'])
        legacy_by_id = {entry['id']: entry for entry in legacy_contract['entries']}
        for entry in indexed_contract['entries']:
            legacy = legacy_by_id[entry['id']]
            members = legacy.pop('schedule_entries')
            count = entry.pop('schedule_entry_count')
            digest = entry.pop('schedule_entries_sha256')
            assert entry == legacy
            assert count == len(members)
            expected = hashlib.sha256()
            for member in members:
                expected.update(json.dumps(member, separators=(',', ':')).encode())
                expected.update(b'\n')
            assert digest == expected.hexdigest()


@pytest.mark.parametrize('field', ['schedule_entry_count', 'schedule_entries_sha256'])
def test_indexed_physical_contract_rejects_resealed_membership_tamper(
    tmp_path, inputs, field
):
    _, out, _ = emit(tmp_path, inputs)
    selector_path = out / 'virtual_board_binding.json'
    selector = json.loads(selector_path.read_text(encoding='utf-8'))
    store = out / selector['artifact']
    with closing(sqlite3.connect(store)) as connection, connection:
        row = connection.execute(
            'SELECT fpga,p_port,schedule_entry_count,schedule_entries_sha256 '
            'FROM binding_physical_ports ORDER BY fpga,p_port LIMIT 1'
        ).fetchone()
        assert row is not None
        value = (
            int(row[2]) + 1
            if field == 'schedule_entry_count'
            else ('0' * 64 if row[3] != '0' * 64 else '1' * 64)
        )
        connection.execute(
            f'UPDATE binding_physical_ports SET {field}=? '
            'WHERE fpga=? AND p_port=?',
            (value, row[0], row[1]),
        )
    selector['store_bytes'] = store.stat().st_size
    selector['store_sha256'] = hashlib.sha256(store.read_bytes()).hexdigest()
    selector_path.write_text(
        json.dumps(selector, separators=(',', ':'), sort_keys=True) + '\n',
        encoding='utf-8',
    )

    binding = read_virtual_binding(selector_path)
    with pytest.raises(
        ValidationError,
        match='physical-port membership disagrees with binding entries',
    ):
        build_virtual_io_physical_contract(
            binding,
            str(row[0]),
            xdc_path=out / str(row[0]) / 'virtual_data_pins.xdc',
            adapter_path=out / str(row[0]) / 'virtual_differential_io.sv',
        )


def test_phase6_streams_adapter_files_without_building_complete_strings(
    tmp_path, inputs, monkeypatch
):
    assert not hasattr(virtual_board_module, 'virtual_binding_to_xdc')
    assert not hasattr(virtual_board_module, 'virtual_binding_to_systemverilog')
    args, out, report = emit(tmp_path, inputs)
    assert validate_phase6(*args, out / 'manifest.json') == report['validation']
    for fpga in ('fpga0', 'fpga1'):
        assert (out / fpga / 'virtual_data_pins.xdc').is_file()
        assert (out / fpga / 'virtual_differential_io.sv').is_file()


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
