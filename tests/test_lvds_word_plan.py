"""Word-mode wiring and Phase 6 bank-plan integration; no physical QoR claim."""
import copy
from collections import defaultdict

import pytest

from emuflow.default_platform import materialize_research_platform
from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.io import read_json,write_json
from emuflow.lvds_word_plan import build_word_phy_plan
from emuflow.phase3_assignment_index import write_phase3_assignment_index
from emuflow.phase6 import run_phase6,validate_phase6
from emuflow.platform import Platform
from emuflow.system_route_storage import write_system_route_storage,read_system_routes
from emuflow.tdm import build_native_exact_schedule_request
from emuflow.tdm_schedule_storage import write_tdm_schedule_storage
from emuflow.transport_storage import read_transport,TransportView
from emuflow.virtual_board import build_virtual_board
from tests.test_native_schedule_generation import install_runtime


@pytest.mark.parametrize('count',(2,4))
def test_word_wiring_separates_clock_pairs_and_preserves_fixed_data_cables(tmp_path,count):
    path=tmp_path/'board.json'
    materialize_research_platform(path,fpga_count=count,transport_format='lvds-word-v2')
    platform=Platform.load(path);board=build_virtual_board(platform)
    assert all(l.word_timing.serial_ui_ns==0.625 for l in platform.links)
    assert board['metrics']['data_lane_pairs']==360*len(platform.links)
    assert board['metrics']['forwarded_clock_pairs']==18*len(platform.links)
    assert board['metrics']['differential_pairs']==378*len(platform.links)
    assert board['metrics']['physical_conductors']==756*len(platform.links)
    pins=defaultdict(set);contacts=defaultdict(set)
    for wire in board['wires']:
        for pair in wire['endpoints']:
            for p in ('p','n'):pins[pair['fpga']].add(pair[p]['package_pin'])
        contacts[wire['cable']].update(wire['connector_data_contacts'].values())
    for clock in board['forwarded_clocks']:
        for pair in clock['endpoints']:
            assert '_GC_QBC_'in pair['p']['pin_function']
            for p in ('p','n'):
                assert pair[p]['package_pin']not in pins[pair['fpga']]
                pins[pair['fpga']].add(pair[p]['package_pin'])
        for n in clock['connector_clock_contacts'].values():
            assert n not in contacts[clock['cable']];contacts[clock['cable']].add(n)
    assert {len(v)for v in contacts.values()}=={126}
    for f in board['fpgas']:
        assert '65'in f['unallocated_hp_banks']
        assert len(pins[f['id']])==42*len(f['bank_assignments'])


def emit_word_case(tmp_path,monkeypatch):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    install_runtime(tmp_path,monkeypatch)
    platform_path=tmp_path/'board.json'
    materialize_research_platform(platform_path,fpga_count=2,transport_format='lvds-word-v2')
    case=StaticExactCombinationalCutPartitionTest();case.setUp();case.platform=Platform.load(platform_path)
    _clusters,assignment=case._exact_artifacts(dependent_return=True)
    ir_path=tmp_path/'design.emuir.sqlite';write_compact_emuir(ir_path,case.ir.value)
    assignment_path=tmp_path/'assignment.json';write_json(assignment_path,assignment)
    write_phase3_assignment_index(tmp_path/'assignment.sqlite',case.ir,assignment)
    route_path=tmp_path/'routes.json';route_value=case._exact_routes(assignment,frame_slots=32)
    route_value['semantic_contract']={**route_value['semantic_contract'],'indexed':True}
    write_system_route_storage(route_path,route_value)
    routes=read_system_routes(route_path);schedule_path=tmp_path/'schedule.json'
    write_tdm_schedule_storage(schedule_path,build_native_exact_schedule_request(routes,case.platform))
    out=tmp_path/'split'
    report=run_phase6(ir_path,assignment_path,schedule_path,platform_path,out,
        equivalence_cycles=2,clock_periods={c['id']:10 for c in case.ir.value['clocks']})
    return (ir_path,assignment_path,schedule_path,platform_path),out,report


def test_phase6_word_plan_is_source_bound_and_uses_indexed_lane_reduction(tmp_path,monkeypatch):
    args,out,report=emit_word_case(tmp_path,monkeypatch)
    manifest=read_json(out/'manifest.json');plan=read_json(out/'word_phy_plan.json')
    assert 'virtual_board_binding'not in manifest
    assert manifest['word_phy_plan']=={'wiring':'virtual_board_wiring.json','plan':'word_phy_plan.json'}
    assert plan['metrics']['active_banks']==4
    assert plan['native_phy']=={'ip_definition':'xilinx.com:ip:high_speed_selectio_wiz:3.6',
        'rx_alignment':'word-one-stage-v1'}
    assert validate_phase6(*args,out/'manifest.json')==report['validation']
    from emuflow.tdm_schedule_storage import read_tdm_schedule
    views={f['fpga']:read_transport(out/f['transport'])for f in manifest['fpgas']}
    old=TransportView.__getitem__
    def without_endpoints(self,key):
        if key=='endpoints':raise AssertionError('word bank planner scanned endpoint JSON')
        return old(self,key)
    monkeypatch.setattr(TransportView,'__getitem__',without_endpoints)
    assert build_word_phy_plan(Platform.load(args[3]),read_json(out/'virtual_board_wiring.json'),
        read_tdm_schedule(args[2]),views)==plan
    monkeypatch.setattr(TransportView,'__getitem__',old)
    for damage in ('lane','alignment'):
        changed=copy.deepcopy(plan)
        if damage=='lane':changed['fpgas'][0]['banks'][0]['logical_lsb']+=8
        else:changed['native_phy']['rx_alignment']='native-bit-slip-v1'
        write_json(out/'word_phy_plan.json',changed)
        with pytest.raises(ValidationError,match='differs from its canonical'):
            validate_phase6(*args,out/'manifest.json')


def test_word_plan_without_an_alignment_selection_is_not_admitted():
    from emuflow.lvds_word_plan import validate_word_phy_plan
    with pytest.raises(ValidationError,match='unsupported word PHY plan schema'):
        validate_word_phy_plan(None,None,None,None,{'schema':'emuflow.lvds-word-phy-plan/v1'})


def test_word_mode_cannot_be_executed_as_the_bit_adapter(tmp_path):
    from emuflow.multi_fpga_physical_flow import run_multi_fpga_physical_flow
    from emuflow.netlist import SPLIT_MANIFEST_SCHEMA
    path=tmp_path/'board.json';materialize_research_platform(path,fpga_count=2,transport_format='lvds-word-v2')
    split=tmp_path/'split';split.mkdir();write_json(split/'manifest.json',{'schema':SPLIT_MANIFEST_SCHEMA})
    with pytest.raises(ValidationError,match='native word physical-top'):
        run_multi_fpga_physical_flow(split,path,tmp_path/'missing-schedule.json',tmp_path/'physical',backend='vivado')
    assert not (tmp_path/'physical').exists()


def test_word_physical_preflight_consumes_phase6_without_global_sta(tmp_path,monkeypatch):
    from emuflow.multi_fpga_physical_flow import _native_word_physical_sources
    from emuflow.tdm_schedule_storage import read_tdm_schedule
    args,out,_report=emit_word_case(tmp_path,monkeypatch)
    platform=Platform.load(args[3]);schedule=read_tdm_schedule(args[2]);manifest=read_json(out/'manifest.json')
    old=TransportView.__getitem__
    def without_endpoints(self,key):
        if key=='endpoints':raise AssertionError('word physical preflight expanded endpoint JSON')
        return old(self,key)
    monkeypatch.setattr(TransportView,'__getitem__',without_endpoints)
    sources=_native_word_physical_sources(out,manifest,platform,schedule,'vivado')
    assert sources=={'wiring':out/'virtual_board_wiring.json','plan':out/'word_phy_plan.json'}
    with pytest.raises(ValidationError,match='requires Vivado'):
        _native_word_physical_sources(out,manifest,platform,schedule,'rapidwright')
    invalid=copy.deepcopy(read_json(sources['plan']));invalid['fpgas'][0]['banks'][0]['logical_lsb']+=8
    write_json(sources['plan'],invalid)
    with pytest.raises(ValidationError,match='differs from its canonical'):
        _native_word_physical_sources(out,manifest,platform,schedule,'vivado')


def test_word_physical_evidence_cannot_mix_sources_or_drop_fixed_clocks():
    from emuflow.multi_fpga_physical_flow import _require_native_word_evidence
    hashes={'word_phy_plan':'a'*64,'word_wiring':'b'*64}
    evidence={'status':'pass','infrastructure_timing':{'fixed_clock_contract_met':True},
        'differential_pairs':42,'preparation':{'fpga':'fpga0','part':'part','data_pairs':40,'forwarded_clock_pairs':2}}
    record={'fpga':'fpga0','part':'part','active':True,'stages':{'word_phy_implementation':evidence,
        'vivado_implementation':{'word_phy_implementation':copy.deepcopy(evidence),
            'provider':'indexed-phase6-vivado-physical-v2',
            'input_identity':{key:{'sha256':value}for key,value in hashes.items()}}}}
    _require_native_word_evidence(record,hashes)
    changed=copy.deepcopy(record);changed['stages']['vivado_implementation']['input_identity']['word_phy_plan']['sha256']='c'*64
    with pytest.raises(ValidationError,match='different Phase6'):_require_native_word_evidence(changed,hashes)
    changed=copy.deepcopy(record);changed['stages']['word_phy_implementation']['infrastructure_timing']['fixed_clock_contract_met']=False
    with pytest.raises(ValidationError,match='evidence.*missing'):_require_native_word_evidence(changed,hashes)
    from emuflow.sampling_cost import SAMPLING_COST_POLICY
    updated=copy.deepcopy(record)
    native=updated['stages']['vivado_implementation']
    native['provider']='indexed-phase6-vivado-word-physical-v3'
    native['native_session']={'timing_query_mode':'clock-aware-reference'}
    native['word_phy_implementation']['sampling_cost_policy']=SAMPLING_COST_POLICY
    updated['stages']['word_phy_implementation']=copy.deepcopy(native['word_phy_implementation'])
    native['local_path_timing']={'import':{'sampling_cost_policy':SAMPLING_COST_POLICY}}
    assert _require_native_word_evidence(updated,hashes)==SAMPLING_COST_POLICY
    native['local_path_timing']['import'].clear()
    with pytest.raises(ValidationError,match='different sampling cost policy'):
        _require_native_word_evidence(updated,hashes)


def test_word_store_policy_must_match_worker_evidence():
    from emuflow.multi_fpga_physical_flow import _require_word_timing_policy
    from emuflow.sampling_cost import SAMPLING_COST_POLICY
    _require_word_timing_policy({}, 'legacy-data-only-v1')
    _require_word_timing_policy({'sampling_cost_policy':SAMPLING_COST_POLICY},SAMPLING_COST_POLICY)
    for header,policy in [({},SAMPLING_COST_POLICY),
                          ({'sampling_cost_policy':SAMPLING_COST_POLICY},'legacy-data-only-v1')]:
        with pytest.raises(ValidationError,match='timing store differs'):
            _require_word_timing_policy(header,policy)


def test_word_format_is_explicit_and_rejects_unsupported_width(tmp_path):
    from emuflow.cli import _build_parser,_dispatch
    path=tmp_path/'word.json'
    args=_build_parser().parse_args(['platform','research-model','--fpga-count','2',
        '--transport-format','lvds-word-v2','--output',str(path)])
    assert _dispatch(args)==0
    assert Platform.load(path).links[0].payload_bits_per_lane_per_cycle==8
    with pytest.raises(ValidationError,match='twenty data pairs'):
        materialize_research_platform(tmp_path/'invalid.json',transport_format='lvds-word-v2',wires_per_bank=16)
    assert not(tmp_path/'invalid.json').exists()
