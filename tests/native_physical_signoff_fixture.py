"""Current typed phase facts for the native physical terminal transaction."""
import copy
import json
import math
import hashlib
from pathlib import Path
import sys

from emuflow.common.io import write_json
from emuflow_tools.stage_clients.schedule_native import run_native_phase5
from emuflow.scheduling.tdm import build_native_exact_schedule_request
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.transport.runtime import build_virtual_runtime, validate_physical_summary, virtual_runtime_controller_to_systemverilog
from emuflow.physical.physical_summary_storage import (
    publish_boundary_identity_store, publish_boundary_timing_store,
    publish_logic_segment_timing_store, publish_local_path_timing_store,
)
from tests.test_global_binding_native import physical_case, add_local_paths
from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
from tests.phase3_store_fixture import write_current_assignment_fixture
from tests.timing_facts import logic_timing_facts
from tests.test_phase7c import Phase7CTest


def make_terminal_case(root, *, members=7, board_kind=None):
    root = Path(root)
    route_root = root / 'system-route'
    route_root.mkdir(parents=True)
    f, _, raw_routes, raw_schedule, physical, routes, old_schedule, indexed = physical_case(route_root, members=members)
    producer = build_native_exact_schedule_request(
        {**routes, 'semantic_contract': {**f.assignment['semantic_contract'], 'indexed': True}},
        f.platform, source_bindings=routes.bound_store_sources())
    run_native_phase5(producer, root / 'tdm')
    schedule = read_tdm_schedule(root / 'tdm/schedule.json')
    # Existing routed facts retain exactly the same hop identities. Only the
    # schedule envelope/qualification is now owned by the real Phase 5 producer.
    assert list(schedule['entries']) == list(old_schedule['entries'])
    for fpga, database in physical['logic_segment_timing'].items():
        database['tdm_schedule_sha256'] = schedule.manifest['logical_pretty_sha256']
        header = {k:v for k,v in database.items() if k not in {'segments','unsupported_member_paths'}}
        indexed['logic_segment_timing'][fpga].close()
        indexed['logic_segment_timing'][fpga] = logic_timing_facts(route_root / f'current-logic-{fpga}.json', header=header, segments=database['segments'])
    add_local_paths(route_root, f, raw_routes, physical, indexed)
    runtime = build_virtual_runtime(schedule, f.platform, {'clk':20.0})
    if board_kind is not None:
        from emuflow.platform.board_link_timing import build_board_link_timing_model
        board_timing=build_board_link_timing_model(f.platform)
        if board_kind!='model-only':
            for row in board_timing['links']:
                row['delay_bound_ns']+=0.125
                row['qualification']=board_kind
                row['source']={'kind':'hardware-measurement' if board_kind=='measured-upper-bound' else 'vendor-characterization',
                    'reference':'independent timing fixture','observations':12}
            board_timing['final_link_timing_signoff']=board_kind=='measured-upper-bound'
        write_json(root/'timing/board-link-timing.json',board_timing)
        physical['board_link_timing']=indexed['board_link_timing']=board_timing
    partition = root / 'partition';partition.mkdir()
    original = StaticExactCombinationalCutPartitionTest();original.setUp()
    write_current_assignment_fixture(partition / 'assignment.json', original.ir, f.assignment)
    fixture=Phase7CTest();fixture.setUp()
    for name, directory in [('phase3', partition), ('phase4', route_root), ('phase6', root / 'split')]:
        directory.mkdir(exist_ok=True)
        value=copy.deepcopy(fixture.reports[name])
        value.update(design=routes['design'], platform=f.platform.name)
        value.setdefault('validation', {})['status']='pass'
        if name=='phase6':value['equivalence']['status']='pass'
        write_json(directory / f'{name}_report.json', value)
    write_json(root / 'split/manifest.json', {'clock_periods':{'clk':20.0}})
    (root / 'split/virtual_runtime_controller.sv').write_text(virtual_runtime_controller_to_systemverilog(runtime))
    board = root / 'frontend/phase1/platform.normalized.json'
    board.parent.mkdir(parents=True)
    write_json(board, f.platform.to_dict())
    summary=fixture._physical_summary()
    summary.update(design=routes['design'], platform=f.platform.name,
        provider=physical['provider'], qualification=physical['qualification'],
        static_exact_clock_uncertainty_ns=physical['static_exact_clock_uncertainty_ns'])
    for item in summary['fpgas']:
        item['clocks'].update(fabric_period_ns=runtime['fabric_clock']['period_ns'], dut_period_ns=runtime['virtual_dut_clock']['nominal_period_ns'])
    summary['validation']=validate_physical_summary(summary,runtime,f.platform)
    physical_root=root/'physical';physical_root.mkdir()
    for key,publish in [('boundary_identities',publish_boundary_identity_store),('boundary_timing',publish_boundary_timing_store),('logic_segment_timing',publish_logic_segment_timing_store),('local_path_timing',publish_local_path_timing_store)]:
        summary[key]={}
        for fpga,view in indexed[key].items():
            _,reference=publish(view.selector_path,physical_root,Path(fpga)/f'{key}.json')
            summary[key][fpga]=reference
    summary.update(logic_segment_system_routes_sha256=routes.storage_manifest['logical_sha256'],
        logic_segment_tdm_schedule_sha256=schedule.manifest['logical_pretty_sha256'],
        logic_segment_semantic_contract_sha256=routes['semantic_contract_sha256'])
    write_json(physical_root/'physical-summary.json',summary)
    from emuflow.timing.global_sta import bind_physical_checks
    metadata={}
    checks=bind_physical_checks(runtime,routes,schedule,indexed,f.platform,metadata=metadata,routes_artifact_sha256=hashlib.sha256((route_root/'routes.json').read_bytes()).hexdigest())
    representatives,_=checks.compressed()
    tsv='endpoint\tarrival_ns\trequired_ns\tslack_ns\n'
    for ordinal,row in enumerate(representatives):
        arrival=math.fsum(row.arcs_ns);required=row.required_ns-row.launch_ns
        tsv+=f'o{ordinal}\t{arrival:.17g}\t{required:.17g}\t{required-arrival:.17g}\n'
    engine=root/'fixture-sta'
    engine.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nprint("OpenSTA 3.1.0 0c6421e022 Copyright")\nPath("measurements.tsv").write_text({tsv!r})\n')
    engine.chmod(0o755)
    return {'engine':engine,'runtime':runtime,'checks':checks,'metadata':metadata,
        'platform':f.platform,'routes':routes,'schedule':schedule,'physical':indexed,
        'summary':summary,'root':root}
