"""Native physical facts -> final events, against independent reference facts."""
import copy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.global_sta import bind_physical_checks
from tests.global_sta_fixture import bind_physical_checks as fixture_bind
from emuflow.boundary_identity_storage import write_boundary_identity_storage
from tests.timing_facts import boundary_timing_facts
from tests.timing_facts import logic_timing_facts
from emuflow.system_route_storage import read_system_routes, pretty_system_routes_sha256
from tests.native_route_seal_fixture import write_route_store_fixture
from emuflow.tdm_schedule_storage import read_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests import test_static_exact_system_timing as fixture_module


@pytest.fixture(autouse=True)
def native_runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def physical_case(tmp_path, *, members=1, uncertainty=0.125, registered=False, mutate=None):
    f = fixture_module.StaticExactSystemTimingTest(); f.setUp()
    runtime, routes, _ = f._system_inputs()
    schedule = copy.deepcopy(f.schedule)
    if registered:
        schedule['transport_semantics'] = 'registered-boundary'
    physical = f._system_physical()
    physical['static_exact_clock_uncertainty_ns'] = uncertainty
    if members > 1:
        member_ids = [f'成员-{i:04d}-😀' for i in reversed(range(members))]
        routes['timing']['paths'][0]['compressed_path_ids'] = member_ids
        for database in physical['logic_segment_timing'].values():
            database['segments'] = [dict(row, id=row['id'] + ':' + member, member_path=member)
                                    for row in database['segments'] for member in member_ids]
            database['coverage'].update(segments=len(database['segments']), member_paths=members)
    entries = {e['id']: e for e in schedule['entries']}
    for fpga, database in physical['boundary_identities'].items():
        for row in database['endpoints']:
            e = entries[row['schedule_entry']]; kind = row['kind']
            row.update(demand=e['demand'], logical_net=e['net'], link=e['link'],
                       peer=e['to'] if kind == 'tx' else e['from'], lane=e['lane'],
                       logical_lane=e['lane'], internal_signal='net:'+e['net'],
                       source_class='dut-net' if kind == 'tx' else 'captured-shadow',
                       merged_ir={'external_port': kind, 'external_port_bit':e['lane'],
                                  'external_net': row['id'], 'logical_net':e['net'],
                                  'boundary_register_instances':[] if kind == 'tx' else ['rx']})
    if mutate is not None:
        mutate(routes, schedule, physical)
    write_route_store_fixture(tmp_path/'routes.json', routes)
    indexed_routes = read_system_routes(tmp_path/'routes.json')
    write_tdm_schedule_storage(tmp_path/'schedule.json', schedule)
    indexed_schedule = read_tdm_schedule(tmp_path/'schedule.json')
    indexed = {k:v for k,v in physical.items() if k not in {'boundary_timing','boundary_identities','logic_segment_timing'}}
    for key, writer in [('boundary_timing',boundary_timing_facts),('boundary_identities',write_boundary_identity_storage)]:
        indexed[key] = {}
        for fpga, database in physical[key].items():
            header = {k:v for k,v in database.items() if k != 'endpoints'}
            if key == 'boundary_timing':
                header['measurement_scope'] = 'partition-boundary'
            indexed[key][fpga] = writer(tmp_path/f'{key}-{fpga}.json', header, database['endpoints'])
    indexed['logic_segment_timing']={}
    for fpga, database in physical['logic_segment_timing'].items():
        header={k:v for k,v in database.items() if k not in {'segments','unsupported_member_paths'}}
        header.update(system_routes_sha256=pretty_system_routes_sha256(indexed_routes),
                      tdm_schedule_sha256=indexed_schedule.manifest['logical_pretty_sha256'])
        database.update(system_routes_sha256=header['system_routes_sha256'], tdm_schedule_sha256=header['tdm_schedule_sha256'])
        indexed['logic_segment_timing'][fpga]=logic_timing_facts(
            tmp_path/f'logic-{fpga}.json', header=header, segments=sorted(database['segments'], key=lambda x:(x['system_path'],x['member_path'],x['cut_index'],x['id'])))
    return f, runtime, routes, schedule, physical, indexed_routes, indexed_schedule, indexed


@pytest.mark.parametrize('members,registered',[(1,False),(7,False),(137,False),(7,True)])
def test_native_complete_event_namespace_matches_reference(tmp_path,members,registered):
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path,members=members,registered=registered)
    expected_meta={}; actual_meta={}
    expected=fixture_bind(runtime,routes,schedule,physical,f.platform,metadata=expected_meta)
    import emuflow.tdm as tdm
    assert not hasattr(tdm, 'open_tdm_schedule_timing_path_stream')
    import emuflow.physical_timing_inputs as physical_inputs
    assert not hasattr(physical_inputs, '_endpoint_delay_database')
    actual=bind_physical_checks(runtime,ir,ischedule,ip,f.platform,metadata=actual_meta)
    try:
        assert list(actual)==list(expected)
        assert dict(actual_meta['paths'])==dict(expected_meta['paths'])
        assert actual_meta['compressed_representative_paths']==expected_meta['compressed_representative_paths']
        assert actual._owner.header['logical_sha256']==expected._owner.header['logical_sha256']
        assert actual._owner.header['path_ids_sha256']==expected._owner.header['path_ids_sha256']
        assert json.loads(actual._owner.connection.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0])['implementation_id']=='native-physical-event-binding-v1'
    finally:
        actual._owner.close();expected._owner.close()


def test_selected_native_physical_facts_never_fallback_to_inline(tmp_path):
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path)
    ip['boundary_timing']['fpga0']=physical['boundary_timing']['fpga0']
    with pytest.raises(ValidationError,match='typed owners'):
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform)


def add_local_paths(tmp_path, f, routes, physical, indexed, *, version=2, pure=False):
    from tests.timing_facts import local_timing_facts
    from emuflow.local_path_timing import path_id_set_sha256
    from tests.test_local_path_timing_storage import _header, _record
    if pure:
        for key in ['boundary_timing','boundary_identities','logic_segment_timing']:
            physical[key]={}; indexed[key]={}
    members = [] if pure else routes['timing']['paths'][0].get('compressed_path_ids', ['path0'])
    ids=['本地-b','本地-a']
    source={**_header()['source'], 'original_paths':len(members)+len(ids),
            'routes_sha256':hashlib.sha256((tmp_path/'routes.json').read_bytes()).hexdigest(),
            'original_path_ids_sha256':path_id_set_sha256(members+ids)}
    physical['local_path_timing']={}; indexed['local_path_timing']={}
    for fpga in ['fpga1','fpga0']:
        header={**_header(), 'design':routes['design'],'fpga':fpga,'source':source,
                'identity_schema':f'emuflow.local-path-identity/v{version}'}
        records=[dict(_record(identity,1.25+i), fpga=fpga) for i,identity in enumerate(ids) if fpga=='fpga0']
        if version==1:
            for row in records:
                for key in ['required_time_ns','measurement','path_pins']: del row[key]
        header['coverage']={'local_paths':len(records)}
        physical['local_path_timing'][fpga]={**header,'paths':sorted(records,key=lambda r:r['id'])}
        indexed['local_path_timing'][fpga]=local_timing_facts(tmp_path/f'local-{fpga}.json',header,records)
    return source


@pytest.mark.parametrize('version,pure',[(1,False),(2,False),(2,True)])
def test_native_local_original_population_and_zero_cross_paths(tmp_path,version,pure):
    mutate=(lambda routes,schedule,physical:routes['timing'].update(paths=[])) if pure else None
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path,members=7,mutate=mutate)
    source=add_local_paths(tmp_path,f,routes,physical,ip,version=version,pure=pure)
    expected_meta={};actual_meta={}
    expected=fixture_bind(runtime,routes,schedule,physical,f.platform,metadata=expected_meta,routes_artifact_sha256=source['routes_sha256'])
    actual=bind_physical_checks(runtime,ir,ischedule,ip,f.platform,metadata=actual_meta,routes_artifact_sha256=source['routes_sha256'])
    try:
        assert list(actual)==list(expected)
        assert actual._owner.header['logical_sha256']==expected._owner.header['logical_sha256']
        assert actual_meta['source_binding']==source
        assert dict(actual_meta['paths'])==dict(expected_meta['paths'])
    finally:
        actual._owner.close();expected._owner.close()


@pytest.mark.parametrize('damage',['missing','extra','duplicate-global','replace','kind','fpga','entry','member'])
def test_native_rejects_semantically_invalid_current_stores(tmp_path,damage):
    def mutate(routes,schedule,physical):
        launch=physical['logic_segment_timing']['fpga0']['segments'][0]
        transition=physical['logic_segment_timing']['fpga1']['segments'][0]
        if damage=='missing': physical['logic_segment_timing']['fpga1']['segments']=[]
        elif damage=='extra':
            physical['logic_segment_timing']['fpga1']['segments'].append(dict(transition,id='extra',system_path='z-extra',member_path='extra'))
        elif damage=='duplicate-global': transition['id']=launch['id']
        elif damage=='replace': launch['replace_tx_endpoint']='__emuflow_tx_wrong'
        elif damage=='kind': launch['kind']='transition'
        elif damage=='fpga': physical['logic_segment_timing']['fpga0']['platform']='wrong'
        elif damage=='entry': physical['boundary_identities']['fpga0']['endpoints'][0]['demand']='wrong-demand'
        elif damage=='member': transition['member_path']='wrong-member'
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path,mutate=mutate)
    with pytest.raises(ValidationError):
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform)


def test_native_preserves_physical_lane_remap_and_clock_domain_errors(tmp_path):
    def mutate(routes,schedule,physical):
        for database in physical['boundary_identities'].values():
            for row in database['endpoints']:
                row['lane']+=1;row['merged_ir']['external_port_bit']=row['lane']
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path,mutate=mutate)
    expected=fixture_bind(runtime,routes,schedule,physical,f.platform)
    actual=bind_physical_checks(runtime,ir,ischedule,ip,f.platform)
    try: assert list(actual)==list(expected)
    finally: actual._owner.close();expected._owner.close()
    runtime['virtual_dut_clocks']={'other':{'nominal_period_ns':100},'another':{'nominal_period_ns':200}}
    with pytest.raises(ValidationError,match='clock'):
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform)


def test_native_rejects_wrong_full_original_source_coverage(tmp_path):
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path)
    source=add_local_paths(tmp_path,f,routes,physical,ip)
    with pytest.raises(ValidationError,match='population/source'):
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform,metadata={},routes_artifact_sha256='0'*64)


@pytest.mark.parametrize('kind',[False, True, 'tree', 'banks', 'wide-ratio'])
def test_native_multicast_relay_and_affine_physical_chains(tmp_path,monkeypatch,kind):
    from tests.test_phase5_timing_native import _timed_case
    from tests.tdm_timing_oracle import path_facts
    from emuflow.runtime import build_virtual_runtime
    from emuflow.board_link_timing import build_board_link_timing_model
    with patch('tests.test_phase5_timing_native.install_runtime'):
        case,routes,semantic,ir,ischedule,schedule=_timed_case(tmp_path,monkeypatch,kind,count=9)
    platform=case.platform
    runtime=build_virtual_runtime(schedule,platform)
    physical={'boundary_identities':{},'boundary_timing':{},'logic_segment_timing':{},
              'static_exact_clock_uncertainty_ns':.25, 'board_link_timing':build_board_link_timing_model(platform)}
    for fpga in [f.id for f in platform.fpgas]:
        identities=[];timing=[]
        for entry in schedule['entries']:
            for role,owner,peer in [('tx',entry['from'],entry['to']),('rx',entry['to'],entry['from'])]:
                if owner!=fpga:continue
                id=f"__emuflow_{role}_{entry['id']}"
                identities.append({'id':id,'kind':role,'schedule_entry':entry['id'],
                    'demand':entry['demand'],'logical_net':entry['net'],'link':entry['link'],
                    'peer':peer,'lane':entry['lane'],'logical_lane':entry['lane'],
                    'internal_signal':'net:'+entry['net'],'source_class':'dut-net' if role=='tx' else 'captured-shadow',
                    'merged_ir':{'external_port':role,'external_port_bit':entry['lane'],
                                 'external_net':id,'logical_net':entry['net'],
                                 'boundary_register_instances':[] if role=='tx' else ['rx']}})
                timing.append({'id':id,'kind':role,'schedule_entry':entry['id'],'delay_ns':.125,
                    'start_object':'start','end_object':'end','measurement':'logical-source-to-tx-port' if role=='tx' else 'rx-port-to-shadow-capture'})
        coverage={'endpoints':len(identities),'tx':sum(r['kind']=='tx' for r in identities),'rx':sum(r['kind']=='rx' for r in identities)}
        common={'status':'pass','design':routes['design'],'platform':platform.name,'fpga':fpga,'provider':'fixture'}
        physical['boundary_identities'][fpga]={**common,'schema':'emuflow.boundary-identity/v1','coverage':{**coverage,'external_port_nets':len(identities)},'endpoints':identities}
        physical['boundary_timing'][fpga]={**common,'schema':'emuflow.boundary-timing/v1','qualification':'fixture','measurement_scope':'partition-boundary','coverage':coverage,'endpoints':timing}
        physical['logic_segment_timing'][fpga]={**common,'schema':'emuflow.logic-segment-timing/v1','qualification':'fixture',
            'coverage':{'segments':0,'system_paths':0,'member_paths':0,'unsupported_member_paths':0},
            'system_routes_sha256':pretty_system_routes_sha256(ir),
            'tdm_schedule_sha256':ischedule.manifest['logical_pretty_sha256'],
            'semantic_contract_sha256':schedule['semantic_contract_sha256'], 'unsupported_member_paths':[], 'segments':[]}
    for path in sorted(path_facts(routes,platform,schedule),key=lambda row:row["path"]):
        for member in path['compressed_path_ids']:
            last=None
            for index,demand in enumerate(path['_cut_demands']):
                hops=[h for h in path['scheduled_hops'] if h['demand']==demand]
                row={'id':f'{member}:{index}','kind':'launch' if index==0 else 'transition',
                     'system_path':path['path'],'member_path':member,'cut_index':index,
                     'fpga':hops[0]['from'],'replace_tx_endpoint':hops[0]['tx_endpoint'],
                     'start_pin':'start','end_pin':'end','delay_ns':.625,'measurement':'endpoint-exact',
                     'static_exact_segment_id':f'segment-{index}'}
                physical['logic_segment_timing'][row['fpga']]['segments'].append(row)
                last=hops[-1]['to']
            row={**row,'id':f'{member}:capture','kind':'capture','cut_index':index+1,'fpga':last,'replace_tx_endpoint':None,'delay_ns':1.375}
            physical['logic_segment_timing'][last]['segments'].append(row)
    indexed={k:v for k,v in physical.items() if k not in {'boundary_timing','boundary_identities','logic_segment_timing'}}
    for key,writer in [('boundary_timing',boundary_timing_facts),('boundary_identities',write_boundary_identity_storage)]:
        indexed[key]={}
        for fpga,database in physical[key].items():
            indexed[key][fpga]=writer(tmp_path/f'{key}-{fpga}.json',{k:v for k,v in database.items() if k!='endpoints'},database['endpoints'])
    indexed['logic_segment_timing']={}
    for fpga,database in physical['logic_segment_timing'].items():
        rows=sorted(database['segments'],key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id']))
        database['coverage'].update(segments=len(rows),system_paths=len({r['system_path'] for r in rows}),member_paths=len({r['member_path'] for r in rows}))
        indexed['logic_segment_timing'][fpga]=logic_timing_facts(tmp_path/f'logic-{fpga}.json',
            header={k:v for k,v in database.items() if k not in {'segments','unsupported_member_paths'}},segments=rows)
    expected=fixture_bind(runtime,routes,schedule,physical,platform)
    actual=bind_physical_checks(runtime,ir,ischedule,indexed,platform)
    try:
        assert list(actual)==list(expected)
        assert actual._owner.header['logical_sha256']==expected._owner.header['logical_sha256']
    finally:
        actual._owner.close();expected._owner.close()


def test_native_rejects_crc_and_retires_invocation_even_with_held_traceback(tmp_path):
    from emuflow import global_binding_native as binding
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path)
    invoke=binding._native;roots=[]
    def corrupt(arguments,root):
        roots.append(root)
        request=Path(arguments[-2]);data=bytearray(request.read_bytes());data[-1]^=1;request.write_bytes(data)
        return invoke(arguments,root)
    with patch.object(binding,'_native',corrupt), pytest.raises(ValidationError) as error:
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform)
    assert error.value is not None
    assert roots and not roots[0].exists()
    assert Path(ir['routes'].store_path).exists()


def test_native_binding_rejoins_saved_measurements_and_publishes_qor_without_python_population(tmp_path):
    from emuflow.global_measurements import import_measurements, read_native_measurements, NativeTimingPaths, NativeMeasurements
    from emuflow.global_sta import build_opensta_timing
    from emuflow.qor_storage import write_qor_report, read_qor_report
    from tests.test_global_measurements import _tsv
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path,members=7)
    reference_meta={};native_meta={}
    reference=fixture_bind(runtime,routes,schedule,physical,f.platform,metadata=reference_meta)
    representatives,_=reference.compressed()
    tsv=tmp_path/'engine.tsv';_tsv(tsv,representatives)
    measurements=import_measurements(tsv,representatives,directory=tmp_path/'measurements')
    measurements.close();reference.close()
    native=bind_physical_checks(runtime,ir,ischedule,ip,f.platform,metadata=native_meta)
    saved=read_native_measurements(tmp_path/'measurements/measurements.json',native)
    try:
        with patch.object(NativeMeasurements,'__iter__',side_effect=AssertionError('Python H scalars')), \
             patch.object(NativeTimingPaths,'__iter__',side_effect=AssertionError('Python P QoR')):
            timing=build_opensta_timing(runtime,native_meta,saved)
            with write_qor_report(tmp_path/'qor.json',{'schema':'emuflow.qor-report/v4','status':'pass','timing':timing}):
                pass
        with read_qor_report(tmp_path/'qor.json') as qor:
            assert qor['timing']['summary']['timing_paths']==7
            assert {path['path'] for path in qor['timing']['paths']}==set(routes['timing']['paths'][0]['compressed_path_ids'])
    finally:
        saved.close();native.close()


@pytest.mark.parametrize('field',['system_routes_sha256','tdm_schedule_sha256','semantic_contract_sha256'])
def test_native_rejects_resealed_physical_evidence_from_another_source(tmp_path,field):
    f,runtime,routes,schedule,physical,ir,ischedule,ip=physical_case(tmp_path)
    original=ip['logic_segment_timing']['fpga0']
    header={**original.selector['header'],field:'0'*64}
    bad=logic_timing_facts(tmp_path/'other-logic.json',header=header,
        segments=sorted(physical['logic_segment_timing']['fpga0']['segments'],key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id'])))
    ip['logic_segment_timing']['fpga0']=bad
    with pytest.raises(ValidationError,match='source seals'):
        bind_physical_checks(runtime,ir,ischedule,ip,f.platform)
