"""Current typed owners plus small physical source facts, never an old query flow."""
from tests.phase3_store_fixture import write_assignment_index_fixture
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from emuflow_tools.stage_clients.schedule_native import run_native_phase5
from emuflow_tools.routing.system_route_storage import read_system_routes
from tests.native_route_seal_fixture import write_route_store_fixture
from emuflow.scheduling.tdm import build_native_exact_schedule_request
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow_tools.timing.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming
from emuflow_tools.stage_clients.opensta import OPENSTA_PROVIDER
from emuflow.transport.split_overlay import build_native_split
from emuflow_tools.transport.transport_storage import write_transport_storage
from emuflow_tools.physical.lowering import run_placement_ir_lowering_from_split
from tests.native_split_fixture import materialize_partition_fixture
from tests.test_placement_lowering_native import synthesized_transport
from tests.test_phase5_native_stage import current_case


def sources(root, *, members=1, extra_launch=False, wrong_owner=False, paths=True, constant_launch=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    case,routes,_=current_case(root)
    # The expanded launch covers a source absent from the representative STA
    # path. Existing cut identities stay unchanged when a local source is added.
    if extra_launch:
        case.ir.value['instances'].append({'id':'q_extra','type':'FDRE','resources':{'ff':1}})
        case.ir.value['nets'].append({'id':'extra','cut_class':'register_output',
            'drivers':[{'instance':'q_extra','port':'Q','bit':0}],
            'sinks':[{'instance':'l0','port':'I1','bit':0}]})
        # Current fixture's fixed cluster target helper knows the original five
        # cells; keep q_extra in the same cluster as q0 using its assignment.
        assignment=deepcopy(case._phase5_assignment)
        assignment['instance_assignment']['q_extra']='fpga0'
        for part in assignment['partitions']:
            if part['fpga']=='fpga0':
                part['resources']['ff']=part['resources'].get('ff',0)+1
        case._phase5_assignment=assignment
    if constant_launch:
        # A real constant-only source cone has no architectural launch. Keep
        # all assignment semantic identities; mark its typed launch contract.
        cell=next(cell for cell in case.ir.value['instances'] if cell['id']=='q0')
        cell.update(type='GND',resources={},parameters={})
        for net in case.ir.value['nets']:
            for endpoint in net['drivers']:
                if endpoint['instance']=='q0':endpoint['port']='G'
            net['sinks']=[endpoint for endpoint in net['sinks']
                          if not (endpoint['instance']=='q0' and endpoint['port']=='C')]
        assignment=deepcopy(case._phase5_assignment)
        for part in assignment['partitions']:
            if part['fpga']=='fpga0':part['resources']['ff']-=1
        launch=next(row for row in assignment['semantic_contract']['logic_segments']
                    if row['kind']=='launch_to_tx' and row['sink_cut_net']=='n0')
        launch['source_semantics']='configuration-stable-constant'
        from emuflow.partition.combinational_cut import semantic_contract_sha256
        assignment['semantic_contract_sha256']=semantic_contract_sha256(assignment['semantic_contract'])
        case._phase5_assignment=assignment
    assignment=deepcopy(case._phase5_assignment)
    original=root/'original.sqlite';write_compact_emuir(original,case.ir.value)
    assignment_path=root/'assignment.sqlite';write_assignment_index_fixture(assignment_path,case.ir,assignment)
    contract=assignment['semantic_contract']
    route_value=deepcopy(__import__('emuflow_tools.document_io',fromlist=['read_json']).read_json(root/'current/routes.json'))
    route_value['semantic_contract_sha256']=assignment['semantic_contract_sha256']
    start='q0/G' if constant_launch else 'q0/Q'
    ids=[f'{start}->q1/D#{i:08d}' for i in range(members)]
    if wrong_owner:ids=[f'l1/O->q1/D#{i:08d}' for i in range(members)]
    if paths:
        route_value['timing']={'schema':'emuflow.sta-paths/v1','normalization':{
            'positive_slack_scale_ns':20.,'negative_slack_scale_ns':20.,'max_clock_period_ns':20.},
            'paths':[{'path':ids[0],'compressed_path_ids':ids,'clock_domain':'clk','clock_period_ns':20.,
                'required_time_ns':18.,'fixed_delay_ns':2.,'cut_nets':['n0','d']}]}
    if not paths:
        route_value.pop('timing',None)
    route_path=root/'current/routes.json';write_route_store_fixture(route_path,route_value)
    routes=read_system_routes(route_path)
    producer=build_native_exact_schedule_request({**routes,'semantic_contract':{**contract,'indexed':True}},
        case.platform,source_bindings=routes.bound_store_sources())
    run_native_phase5(producer,root/'schedule')
    schedule_path=root/'schedule/schedule.json';schedule=read_tdm_schedule(schedule_path)
    raw=root/'paths.tsv';raw.write_text(STA_PATH_DATABASE_TSV_HEADER+'\n'+''.join(
        '\t'.join((identity.encode().hex(),b'clk'.hex(),'20','1','2',','.join(n.encode().hex() for n in ['q','n0','n1','d'])))+'\n'
        for identity in ids))
    database=root/'paths.json';import_sta_path_database_tsv_streaming(raw,original,database,provider=OPENSTA_PROVIDER)
    split=root/'split.sqlite';build_native_split(split,original,assignment_path,schedule,
        expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    transports=write_transport_storage(root/'transport',assignment_path,schedule,case.platform,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'])
    merged={};boundaries={}
    for fpga in ('fpga0','fpga1'):
        transport=transports['transports'][fpga]
        ir=synthesized_transport(transport.materialize())
        synthesized=root/(fpga+'.transport.sqlite');write_compact_emuir(synthesized,ir.to_dict())
        output=root/fpga/'placement.sqlite'
        run_placement_ir_lowering_from_split(split,fpga,transport.manifest_path,synthesized,output)
        merged[fpga]=output;boundaries[fpga]=output.with_name('boundary-identities.json')
    return SimpleNamespace(original=original,assignment=assignment_path,paths=database,routes=route_path,
        schedule=schedule_path,platform=case.platform,merged=merged,boundaries=boundaries,
        members=ids,assignment_value=assignment,case=case)


def prepare(value):
    from emuflow_tools.timing.logic_segment_timing import prepare_logic_segment_query_inputs
    return prepare_logic_segment_query_inputs(value.original,value.assignment,value.paths,
        value.routes,value.schedule,value.platform)


def query(value,root,*,backend='xilinx',fpga='fpga0',prepared=None):
    from emuflow_tools.timing.logic_segment_timing import write_xilinx_logic_segment_query, write_vivado_logic_segment_query
    function=write_xilinx_logic_segment_query if backend=='xilinx' else write_vivado_logic_segment_query
    return function(value.original,value.assignment,value.paths,value.routes,value.schedule,value.platform,
        value.merged[fpga],value.boundaries[fpga],fpga,Path(root)/'query.tsv',Path(root)/'identity.json',
        prepared_inputs=prepared)
