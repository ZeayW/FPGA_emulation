from tests.compile_adapter_fixture import preflight as run_preflight
"""Native/SQL contract fixtures, not placement, QoR or NVDLA qualification."""

from tests.schedule_store_fixture import write_tdm_schedule_storage
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.timing.boundary_identity_storage import write_boundary_identity_storage
from emuflow.transport.chimew.chimew_fixed_wiring import FixedWiringChannels, FIXED_WIRING_MAP_SCHEMA
from emuflow.transport.chimew.chimew_lookahead import materialize_xilinx_chimew_inputs
from emuflow.transport.chimew.chimew_pipeline import run_chimew_phase6_pipeline
from emuflow.platform.default_platform import research_platform_path
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json, file_sha256

from tests.vivado_lineage_host import import_cell_lineage, PLACEMENT_COORDINATE_SYSTEM
from emuflow.transport.chimew.geometry import XilinxChimewGeometry
from tests.chimew_geometry_reference import XilinxChimewGeometry as GeometryReference, read_package_geometry_reference
from tests.test_chimew_fixed_wiring import fixed_case


@pytest.fixture(params=["vivado", "rapidwright"])
def case(tmp_path, fixed_case, request):
    from tests.test_vivado_lineage import VivadoLineageTests
    platform, schedule, _problem, _electrical = fixed_case
    source_root = tmp_path / 'original'; source_root.mkdir()
    VivadoLineageTests().prepare(source_root)
    source = source_root / 'source.emuir.sqlite'
    pins = {}
    fixed = FixedWiringChannels(platform)
    for wire in fixed.board['wires']:
        for endpoint in wire['endpoints']:
            for polarity in ('p','n'):
                pin = endpoint[polarity]
                pins[pin['package_pin']] = {'package_pin': pin['package_pin'], 'site': pin['site'], 'bel': 'PAD',
                    'native_tile': {'coordinate_system': PLACEMENT_COORDINATE_SYSTEM,
                                    'column': 20+int(endpoint['bank']), 'row': 10+wire['wire']*2+(polarity=='n')}}
    architecture = tmp_path / 'architecture.json'
    write_json(architecture, {'schema':'emuflow.archdb/v1', 'part': platform.fpgas[0].part,
        'coordinate_transform':{'name':'tile-grid-with-site-stride-v1'},
        'physical_region_model':{'io_bank_encoded':True,'overlay':{
            'schema':'emuflow.physical-region-sidecar/v1','sha256':'f'*64,
            'producer':'contract-fixture','producer_version':'1'}},
        'sites': [{'name': 'SKIP-ME', 'large-unused-payload': [0]*10000}],
        'physical_regions': {'io_banks': [{'name': 'mocked-native-geometry', 'pins': list(pins.values())}]}})
    records = []
    for index, fpga in enumerate(platform.fpgas):
        root = tmp_path / fpga.id; root.mkdir()
        report, mapped, functions, inventory = VivadoLineageTests().prepare(root)
        lines = inventory.read_text().splitlines()
        # Preserve two physical descendants for the sink, one source object.
        inventory.write_text('\n'.join([lines[0], *(lines[1:2] if index==0 else lines[2:])])+'\n')
        coverage = import_cell_lineage(mapped, functions, {'routed': inventory}, root/'lineage.sqlite',
                                       mapped_sha256=report['mapped_index']['sha256'])
        boundary = root/'boundary.json'
        document = {'schema': 'emuflow.boundary-identity/v1', 'status':'pass', 'design':'dut',
            'platform':platform.name, 'fpga':fpga.id, 'provider':'contract-fixture',
            'coverage': {'endpoints':0,'tx':0,'rx':0,'external_port_nets':0}, 'endpoints': []}
        document.pop("endpoints")
        view = write_boundary_identity_storage(boundary, document, []); view.close()
        stages = {'placement_ir':{'boundary_identity':{'output':str(boundary)}}}
        if request.param == 'vivado':
            stages.update(mapped_verilog=report, vivado_implementation={'cell_coverage':coverage})
        else:
            from tests.physical_handoff_fixture import write_bridge_cluster_fixture
            from tests.openparf_input_reference import _write_site_database
            handoff, sites = root/'handoff.sqlite', root/'site-map.sqlite3'
            write_bridge_cluster_fixture(handoff, {'id':'c0','kind':'slice','site_templates':['SLICEL'],
                    '_placement':{'site':'S0','site_type':'SLICEL','x':9999,'y':8888,'fixed':False},
                    'assignments':[{'instance':'launch' if index==0 else 'capture','cell_type':'FDRE',
                        'bel':'AFF','_placement_mode':'direct','_physical_site':'certified-native-alias','_placement_ordinal':0}]},
                {'schema':'emuflow.packed-site-netlist/v1','top':'dut',
                    'source':{'mapped_json_sha256':report['mapped_index']['sha256'],
                              'architecture_sha256':file_sha256(architecture)}},
                    {'schema':'emuflow.xilinx-placement/v1','part':platform.fpgas[0].part})
            _write_site_database(sites, {'coordinate_system':PLACEMENT_COORDINATE_SYSTEM,
                'architecture_sha256':file_sha256(architecture), 'sites':[{
                    'dense_x':0,'dense_y':0,'physical_x':3+index*10,'physical_y':4+index*10,
                    'placement_x':9999,'placement_y':8888,'site':'S0',
                    'resources':{'FF':16},'physical_sites':{'FF':['S0']}}]})
            from emuflow.common.native_tools import native_file_generation
            stages['rapidwright_implementation']={
                'placement':{'site_database':{'schema':'emuflow.openparf-atomic-site-database/v1',
                    'path':str(sites),'generation':list(native_file_generation(sites)),'sha256':file_sha256(sites)}},
                'result':{'artifacts':{'mapped':report['mapped_index'],
                    'physical_handoff':{'path':str(handoff),'sha256':file_sha256(handoff)}}}}
        records.append({'fpga':fpga.id,'status':'pass','active':True,'stages':stages})
    schedule_path = tmp_path/'schedule.json'
    for e in schedule['entries']:
        e.update(net='n5', capacity_key=f"{e['link']}:{e['from']}:{e['to']}",
                 ready_slot=e['slot'], arrival_slot=e['slot'] + 1)
    for section in ("routes", "demand_completions", "domain_schedules"):
        schedule.setdefault(section, [])
    write_tdm_schedule_storage(schedule_path, schedule)
    routes = tmp_path/'routes.json'
    from tests.native_route_seal_fixture import write_route_store_fixture
    write_route_store_fixture(routes, {'schema':'emuflow.system-routes/v1',
        'design':schedule['design'], 'platform':schedule['platform'], 'routes':[], 'demands':[]})
    return platform, source, architecture, {'fpgas':records}, schedule_path, routes



def geometry_case(case,**options):
    platform,source,architecture,report,schedule,*_=case
    return XilinxChimewGeometry(source,research_platform_path(len(platform.fpgas)),report,architecture,
        schedule_path=schedule,**options)


def selected_entries(case,entries):
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    path=case[4]
    with closing(read_tdm_schedule(path)) as view:
        header=view.native_validation_header()
        template=dict(next(iter(view['entries'])))
    rows=[dict(template,**entry) for entry in entries]
    write_tdm_schedule_storage(path,{**header,'entries':rows,'routes':[],
        'demand_completions':[],'domain_schedules':[]})

def test_selected_native_locations_and_full_paper_pipeline(case, tmp_path):
    platform, source, architecture, report, schedule, routes = case
    initial_schedule_hash = file_sha256(schedule)
    from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
    catalog=XilinxPackageGeometry.open(architecture,platform.fpgas[0].part)
    assert dict(catalog.pins)==read_package_geometry_reference(architecture,platform.fpgas[0].part)
    with geometry_case(case) as geometry:
        for fpga, kind in ((platform.fpgas[0].id, 'tx'), (platform.fpgas[1].id, 'rx')):
            point = geometry.point(fpga, 'n5', 's0', kind)
            assert all(isinstance(v, float) for v in point)
            assert 0 <= point[2] <= 1
            if "rapidwright_implementation" in report["fpgas"][0]["stages"]:
                index = 0 if kind == "tx" else 1
                assert point[:2] == (3+index*10,4+index*10)
        assert geometry.forwarded == 0
        from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
        with GeometryReference(source,platform,report,architecture) as reference, closing(read_tdm_schedule(schedule)) as selected:
            for entry in selected['entries']:
                for kind,owner in (('tx',entry['from']),('rx',entry['to'])):
                    assert geometry.point(owner,entry['net'],entry['id'],kind)==reference.point(owner,entry['net'],entry['id'],kind)
        assert geometry.header['implementation_id']=='native-indexed-xilinx-geometry-v1'
        assert geometry.header['metrics']['points']==geometry._db.execute('SELECT count(*) FROM points').fetchone()[0]
        assert geometry._db.execute('SELECT name FROM sqlite_temp_master').fetchall()==[]
    lookahead = materialize_xilinx_chimew_inputs(ir_path=source, schedule_path=schedule,
        routes_path=routes, platform_path=research_platform_path(2), architecture_path=architecture,
        physical_report=report, output_dir=tmp_path/'lookahead')
    artifacts = lookahead['artifacts']
    electrical = read_json(Path(artifacts['electrical_map']['path']))
    assert electrical['schema'] == FIXED_WIRING_MAP_SCHEMA
    assert all(c['electrical_class']=='fixed-lvds-pair' for c in electrical['channels'])
    assert Path(artifacts['schedule']['path']) == schedule
    assert file_sha256(schedule) == initial_schedule_hash
    placement = read_json(Path(lookahead['sources']['placement']))
    assert all('instances' not in fpga for fpga in placement['fpgas'])
    assert not (tmp_path/'lookahead/sources/placement.json').exists()
    result = run_chimew_phase6_pipeline(*[Path(artifacts[k]['path']) if k!='platform' else research_platform_path(2)
        for k in ('schedule','platform','crossings','positions','rudy_input','bank_channel_input','electrical_map')],
        tmp_path/'pipeline', source_paths={k:Path(v) for k,v in lookahead['sources'].items()}, region_count=4)
    assert result['status'] == 'pass'


@pytest.mark.parametrize('mutation',['part','missing','wrong-site','negative','boolean','duplicate'])
def test_geometry_rejects_missing_or_forged_fixed_pin_facts(case, mutation):
    platform, source, architecture, report, *_ = case
    value=read_json(architecture)
    pins=value['physical_regions']['io_banks'][0]['pins']
    if mutation=='part': value['part']='wrong'
    elif mutation=='missing': pins[0].pop('native_tile')
    elif mutation=='wrong-site': pins[0]['site']='other'
    elif mutation=='negative': pins[0]['native_tile']['row']=-1
    elif mutation=='boolean': pins[0]['native_tile']['column']=True
    else: pins.append(copy.deepcopy(pins[0]))
    write_json(architecture,value)
    with pytest.raises(ValidationError):
        geometry_case(case)


def test_missing_active_placement_is_not_a_synthetic_centre(case):
    platform, source, architecture, report, *_=case
    with geometry_case(case) as geometry:
        with pytest.raises(ValidationError,match='selected schedule endpoint'):
            geometry.point(platform.fpgas[0].id,'n7','s0','rx')


def test_generation_mutation_is_rejected(case):
    platform, source, architecture, report, *_=case
    with pytest.raises(ValidationError,match='generation changed'):
        with geometry_case(case):
            architecture.write_text(architecture.read_text()+'\n')


def test_wrong_mapped_generation_seal_is_rejected(case):
    platform,source,architecture,report,*_=case
    stages=report['fpgas'][0]['stages']
    if 'mapped_verilog' in stages:
        stages['mapped_verilog']['mapped_index']['sha256']='0'*64
    else:
        stages['rapidwright_implementation']['result']['artifacts']['mapped']['sha256']='0'*64
    with pytest.raises(ValidationError,match='placement/source identity'):
        geometry_case(case)


def test_timing_guard_has_one_owner_without_cloning_the_hop_store(case, tmp_path):
    from tests.chimew_grouping_fixture import build_chimew_initial_groups
    from tests.chimew_refinement_fixture import refine_chimew_groups
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    platform, source, architecture, report, schedule, routes = case
    original = file_sha256(schedule)
    timing = tmp_path/'critical.json'
    write_json(timing, {'schema':'emuflow.sta-paths/v1','design':'chimew_fixture','paths':[
        {'id':'p0','clock_period_ns':10,'slack_ns':-1,'cut_nets':['n5']}]})
    lookahead = materialize_xilinx_chimew_inputs(ir_path=source,schedule_path=schedule,routes_path=routes,
        platform_path=research_platform_path(2),architecture_path=architecture,physical_report=report,
        timing_paths_path=timing,output_dir=tmp_path/'guarded')
    assert file_sha256(schedule) == original
    crossing = read_json(Path(lookahead['artifacts']['crossings']['path']))
    assert crossing['timing_guard']['protected_entries']
    with closing(read_tdm_schedule(schedule)) as schedule_view:
        initial = build_chimew_initial_groups(schedule_view,crossing)
        initial['entries'][0].pop('timing_guard_lane',None)
        positions = read_json(Path(lookahead['artifacts']['positions']['path']))
        with pytest.raises(ValidationError,match='declared lookahead guards'):
            refine_chimew_groups(schedule_view,crossing,initial,positions)


@pytest.mark.parametrize('backend',['vivado','rapidwright'])
def test_prepass_and_final_share_the_selected_backend_options(tmp_path,backend):
    from tests.test_compile_preflight_native import request_options,run
    from emuflow.flow.native_compile import _request
    from tests.native_compile_state_fixture import _unpack
    from tests.native_build import native_pipeline_api_test
    import subprocess
    options=request_options(tmp_path,backend);options.update(physical_seed=1,physical_workers=4)
    value,_=_request((),options);result,state,operation=run(tmp_path,value)
    assert result.returncode==0,operation
    policy=_unpack(json.loads(state.read_text())['state'])['native_physical_policy']
    before=policy.read_bytes();generated=[]
    for arm in ('baseline','candidate'):
        manifest=tmp_path/(arm+'-split.json');manifest.write_text('{}')
        output=tmp_path/(arm+'-request.json')
        result=subprocess.run([str(native_pipeline_api_test()),'physical-compile-request',str(policy),str(manifest),str(output)],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        request=json.loads(output.read_text());generated.append(request)
        assert request['backend']==backend and request['seed']==1 and request['workers']==4
    for field in ('backend','backend_options','workers','seed'):
        assert generated[0][field]==generated[1][field]
    assert [row for row in generated[0]['sources'] if row['role']!='split-manifest']==[row for row in generated[1]['sources'] if row['role']!='split-manifest']
    assert policy.read_bytes()==before


def test_chimew_preflight_accepts_both_xilinx_backends_before_frontend(case,tmp_path,request):
    from tests.chimew_admission_fixture import admitted
    from tests.native_compile_state_fixture import _unpack
    backend=request.node.callspec.params['case']
    _request,state=admitted(case,tmp_path/'preflight',backend)
    value=_unpack(json.loads(state.read_text())['state'])
    assert json.loads(value['native_chimew_configuration'].read_text())['package_geometry']['part']==case[0].fpgas[0].part
    assert value['physical_backend']==backend
    assert not (tmp_path/'preflight/native/frontend/phase1').exists()


def test_multicast_query_cache_is_bounded_and_uses_indexes(case):
    platform,source,architecture,report,*_=case
    with closing(sqlite3.connect(source)) as db,db:
        seq=db.execute("SELECT seq FROM net_records WHERE id='n5'").fetchone()[0]
        first=db.execute('SELECT max(seq)+1 FROM net_records').fetchone()[0]
        for index in range(4100):
            db.execute('INSERT INTO net_records SELECT ?,?,name,flags,yosys_bit,bus_index,fanout,cut_class,extras_seq FROM net_records WHERE seq=?',
                       (first+index,f'repeated-{index}',seq))
            db.execute('INSERT INTO endpoint_records SELECT ?,role,ordinal,instance_seq,port_seq,bit,extras_seq FROM endpoint_records WHERE net_seq=?',
                       (first+index,seq))
    selected_entries(case,[{'id':f'selected-{i:05}','demand':f'demand-{i:05}','net':f'repeated-{i}'} for i in range(4100)])
    with geometry_case(case) as geometry:
        assert geometry.header['metrics']['points']==8200
        assert geometry.header['metrics']['source_cache_peak']==4096
        assert geometry.header['metrics']['source_queries']==8200
        for plan in geometry.header['lookup_plans'].values():
            plan=' '.join(plan)
            assert 'SCAN cell' not in plan and 'SCAN a' not in plan
            if 'rapidwright_implementation' in report['fpgas'][0]['stages']:
                assert 'physical_sites_name' in plan
        fpga=platform.fpgas[0].id
        assert geometry.point(fpga,'repeated-4099','selected-04099','tx')[0]>=0


@pytest.mark.parametrize('registers',[['capture'],['capture','missing']])
def test_forwarded_hop_uses_complete_source_bound_register_geometry(case, registers):
    platform,source,architecture,report,*_=case
    record=report['fpgas'][1]
    boundary=Path(record['stages']['placement_ir']['boundary_identity']['output'])
    header=read_json(boundary)['header']
    endpoint={'id':'tx-forwarded','kind':'tx','schedule_entry':'relay','demand':'d0',
        'logical_net':'n5','link':platform.links[0].id,'peer':platform.fpgas[0].id,
        'lane':0,'logical_lane':0,'internal_signal':'shadow','source_class':'forwarded-shadow',
        'merged_ir':{'external_port':'tx','external_port_bit':0,'external_net':'wire0','logical_net':'n5',
                     'boundary_register_instances':registers}}
    view=write_boundary_identity_storage(boundary,header,[endpoint]);view.close()
    return_boundary=Path(report['fpgas'][0]['stages']['placement_ir']['boundary_identity']['output'])
    receiver=copy.deepcopy(endpoint);receiver.update(id='rx-forwarded',kind='rx',peer=platform.fpgas[1].id,source_class='captured-shadow')
    receiver['merged_ir'].update(external_port='rx',boundary_register_instances=['launch'])
    view=write_boundary_identity_storage(return_boundary,read_json(return_boundary)['header'],[receiver]);view.close()
    selected_entries(case,[{'id':'relay','demand':'d0','from':platform.fpgas[1].id,'to':platform.fpgas[0].id}])
    if len(registers)>1:
        with pytest.raises(ValidationError,match='boundary coverage'):
            geometry_case(case)
        return
    with GeometryReference(source,platform,report,architecture) as reference, geometry_case(case) as geometry:
        expected=reference.point(platform.fpgas[1].id,'n5','original','rx')
        assert geometry.point(platform.fpgas[1].id,'n5','relay','tx')==expected
        assert geometry.forwarded==2
        with pytest.raises(ValidationError,match='selected schedule endpoint'):
            geometry.point(platform.fpgas[1].id,'wrong-net','relay','tx')


def test_preflight_catalog_is_reused_without_rescanning_the_device_graph(case):
    from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
    platform,source,architecture,report,*_=case
    catalog=XilinxPackageGeometry.open(architecture,platform.fpgas[0].part)
    with patch('emuflow.transport.chimew.xilinx_chimew_geometry._native_package_geometry',side_effect=AssertionError('device graph reread')):
        with geometry_case(case,package_geometry=catalog) as geometry:
            geometry.point(platform.fpgas[0].id,'n5','s0','tx')


@pytest.mark.parametrize("case",["rapidwright"],indirect=True)
def test_shared_site_map_seal_is_published_once_and_survives_later_hardlinks(case, tmp_path):
    import os
    from emuflow.common.native_tools import native_file_generation
    from tests.openparf_input_reference import _materialize_site_database
    platform,source,architecture,report,*_=case
    device={'coordinate_system':{'coordinate_system':PLACEMENT_COORDINATE_SYSTEM,
        'architecture_sha256':file_sha256(architecture),'sites':[
            {'dense_x':index,'dense_y':0,'physical_x':3+index*10,'physical_y':4+index*10,
             'placement_x':9999,'placement_y':8888,'site':f'S{index}',
             'resources':{'FF':16},'physical_sites':{'FF':[f'S{index}']}} for index in range(2)]}}
    with patch('tests.openparf_input_reference._sha256',wraps=file_sha256) as producer_hash:
        for index,record in enumerate(report['fpgas']):
            stage=record['stages']['rapidwright_implementation']
            ref=stage['placement']['site_database']; path=Path(ref['path'])
            _materialize_site_database(path,device)
            _materialize_site_database(path,device)  # Idempotent publication keeps the inode.
            ref.update(generation=list(native_file_generation(path)),sha256=device['site_database_sha256'])
            handoff=Path(stage['result']['artifacts']['physical_handoff']['path'])
            with closing(sqlite3.connect(handoff)) as db,db:
                db.execute('UPDATE cluster SET site=?',(f'S{index}',))
            stage['result']['artifacts']['physical_handoff']['sha256']=file_sha256(handoff)
    assert producer_hash.call_count==1
    first=Path(report['fpgas'][0]['stages']['rapidwright_implementation']['placement']['site_database']['path'])
    os.link(first,tmp_path/'later-reader.sqlite')
    with geometry_case(case) as geometry:
        assert geometry.point(platform.fpgas[0].id,'n5','s0','tx')[:2]==(3,4)
        assert geometry.point(platform.fpgas[1].id,'n5','s0','rx')[:2]==(13,14)
        assert geometry.header['metrics']['site_map_seal_rechecks']==1


@pytest.mark.parametrize("case",["rapidwright"],indirect=True)
def test_site_map_edit_cannot_hide_behind_restored_mtime(case):
    import os
    platform,source,architecture,report,*_=case
    ref=report['fpgas'][0]['stages']['rapidwright_implementation']['placement']['site_database']
    path=Path(ref['path']); before=path.stat()
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('UPDATE sites SET physical_x=99')
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(ValidationError,match='content seal disagrees'):
        geometry_case(case)


@pytest.mark.parametrize('case',['rapidwright'],indirect=True)
def test_site_map_publisher_rejects_a_changed_shared_generation(case, tmp_path):
    import os
    from tests.openparf_input_reference import _materialize_site_database
    from emuflow.common.native_tools import native_file_generation
    platform,source,architecture,report,*_=case
    path=Path(report['fpgas'][0]['stages']['rapidwright_implementation']['placement']['site_database']['path'])
    device={'site_database':path,'site_count':1,'site_database_sha256':file_sha256(path),
            'site_database_generation':native_file_generation(path)}
    before=path.stat()
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('UPDATE sites SET physical_y=99')
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    destination=tmp_path/'must-not-publish.sqlite'
    with pytest.raises(ValidationError,match='changed after publication'):
        _materialize_site_database(destination,device)
    assert not destination.exists()


@pytest.mark.parametrize('damage', ['unknown-schema','duplicate-role','source-generation',
    'catalog-namespace','catalog-generation','schedule-selector-escape',
    'boundary-selector-escape','late-entry-point','late-schedule-order','physical-sha'])
def test_native_geometry_rejects_forged_inputs_without_publishing_a_partial_owner(case,tmp_path,monkeypatch,damage):
    import emuflow.transport.chimew.geometry as host
    from emuflow.common.native_tools import native_file_generation
    if damage.startswith('late-'):
        selected_entries(case,[{'id':'s0','demand':'d0'},{'id':'s1','demand':'d1'}])
    real=host.NativeToolProcess
    def corrupt(native,argv,**options):
        index=argv.index('prepare-chimew-geometry')
        request=Path(argv[index+1]);value=read_json(request)
        if damage=='unknown-schema':value['schema']='emuflow.chimew-geometry-input/unknown'
        elif damage=='duplicate-role':value['sources'][-1]=copy.deepcopy(value['sources'][0])
        elif damage=='source-generation':value['sources'][1]['generation'][2]+=1
        elif damage=='catalog-namespace':value['package_geometry']['path']=str(case[1])
        elif damage=='catalog-generation':value['package_geometry']['generation'][2]+=1
        elif damage=='physical-sha':
            stages=value['physical_report']['fpgas'][0]['stages']
            if 'vivado_implementation' in stages:stages['vivado_implementation']['cell_coverage']['sha256']='f'*64
            else:stages['rapidwright_implementation']['result']['artifacts']['physical_handoff']['sha256']='f'*64
        elif damage=='boundary-selector-escape':
            path=Path(value['physical_report']['fpgas'][0]['stages']['placement_ir']['boundary_identity']['output'])
            selector=read_json(path);selector['artifact']='../escape.sqlite';write_json(path,selector)
        else:
            source=next(row for row in value['sources'] if row['role']=='schedule')
            path=Path(source['path']);selector=json.loads(path.read_text())
            if damage=='schedule-selector-escape':selector['artifact']='../escape.sqlite'
            else:
                store=path.parent/selector['artifact']
                with closing(sqlite3.connect(store)) as db,db:
                    if damage=='late-entry-point':db.execute("UPDATE schedule_entries SET net='absent-net' WHERE seq=1")
                    else:db.execute('UPDATE schedule_entries SET seq=seq+1000')
                digest=file_sha256(store);destination=store.with_name('schedule.'+digest+'.sqlite')
                store.rename(destination);selector.update(artifact=destination.name,store_sha256=digest,store_bytes=destination.stat().st_size)
            write_json(path,selector);source['generation']=list(native_file_generation(path))
        write_json(request,value)
        return real(native,argv,**options)
    monkeypatch.setattr(host,'NativeToolProcess',corrupt)
    output=tmp_path/'rejected-native-owner'
    with pytest.raises(ValidationError):geometry_case(case,output_dir=output)
    assert not output.exists()
    assert not list(tmp_path.glob('.chimew-geometry-*'))


def test_native_geometry_does_not_replace_a_published_owner(case,tmp_path):
    output=tmp_path/'preserved'
    with geometry_case(case,output_dir=output) as view:
        original=file_sha256(view.store_path)
        assert {row['schema'] for row in view.header['input_artifacts']} >= {
            'emuir-compact-v2','tdm-schedule-selector-v4','tdm-schedule-store-v4'}
    with pytest.raises(ValidationError,match='already exists'):
        geometry_case(case,output_dir=output)
    assert file_sha256(output/'geometry.sqlite')==original


@pytest.mark.parametrize('scope', ['exact-hop', 'whole-net'])
@pytest.mark.parametrize('scale,slacks', [(9.0, [-4.0, -2.0]), (0.0, [1.0]), (2.5, [1.0, 3.0])])
def test_native_signal_weights_match_independent_policy(case, tmp_path, scope, scale, slacks):
    from tests.chimew_signal_reference import _timing_weights, _crossed_boundaries
    from emuflow.transport.chimew.signals import ChimewSignals
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    from tests.native_route_seal_fixture import write_route_store_fixture
    platform, source, architecture, report, schedule_path, routes_path = case
    selected_entries(case, [{'id': 'selected'}])
    with closing(read_tdm_schedule(schedule_path)) as schedule:
        row = dict(next(iter(schedule['entries'])))
        timing = {'schema': 'emuflow.sta-paths/v1', 'design': schedule['design'], 'paths': [
            {'id': f'p{i}', 'clock_period_ns': 4.0, 'slack_ns': slack,
             'normalized_slack': slack / 8.0, 'cut_nets': ['n5'] if i else []}
            for i, slack in enumerate(slacks)]}
        logical = {'schema': 'emuflow.system-routes/v1', 'design': schedule['design'],
                   'platform': schedule['platform'], 'demands': [{'id': 'd0'}],
                   'routes': [{'id': 'd0', 'net': 'n5', 'source': row['from'],
                       'sinks': [row['to']], 'width_bits': 1,
                       'tree_edges': [{'link': row['link'], 'from': row['from'], 'to': row['to']}]}],
                   'timing': {'paths': [{'path': f'p{i}', 'cut_transitions': [
                       {'net': 'n5', 'from': row['from'], 'to': row['to']}]} for i in range(len(slacks))]}}
        write_route_store_fixture(routes_path, logical)
        timing_path = tmp_path / 'projected.json'; write_json(timing_path, timing)
        expected, _, coverage = _timing_weights(timing_path, schedule, logical, scale=scale, path_scope=scope)
    with geometry_case(case) as geometry, ChimewSignals(geometry, schedule_path, routes_path,
            tmp_path / 'native-signals', timing=timing_path, weight_scale=scale,
            path_scope=scope, region_count=31) as native:
        assert dict(native.weights) == expected
        assert native.coverage == coverage
        for entry in native.crossing_entries():
            for kind, fpga, side in [('tx', row['from'], 'source'), ('rx', row['to'], 'sink')]:
                point = geometry.point(fpga, 'n5', row['id'], kind)
                channel = geometry.fixed.channel(row['link'], row['lane'], row['from'], row['to'])
                pin = channel['package_pin_a' if kind == 'tx' else 'package_pin_b']
                box = geometry.bounds[fpga]
                expected_slls = _crossed_boundaries(point[2],
                    (geometry.pins[fpga, pin][1] - box[2]) / max(1, box[3] - box[2]), 30)
                assert entry[side + '_slls'] == expected_slls
        assert native.header['implementation_id'] == 'native-chimew-signal-preparation-v1'
        assert native._db.execute('SELECT name FROM sqlite_temp_master').fetchall() == []
    with pytest.raises(ValidationError, match='closed'):
        native.weights['selected']


@pytest.mark.parametrize('mutation', ['design', 'empty', 'boolean-period', 'zero-period',
    'bad-slack', 'boolean-normalized', 'invalid-cut', 'duplicate-root', 'truncated'])
def test_native_signal_rejects_bad_timing_atomically(case, tmp_path, mutation):
    from emuflow.transport.chimew.signals import ChimewSignals
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    with closing(read_tdm_schedule(case[4])) as schedule:
        timing = {'schema': 'emuflow.sta-paths/v1', 'design': schedule['design'],
                  'paths': [{'clock_period_ns': 4.0, 'slack_ns': -1.0, 'cut_nets': ['n5']}]}
    path = tmp_path / 'bad-timing.json'
    row = timing['paths'][0]
    if mutation == 'design': timing['design'] = 'foreign'
    elif mutation == 'empty': timing['paths'] = []
    elif mutation == 'boolean-period': row['clock_period_ns'] = True
    elif mutation == 'zero-period': row['clock_period_ns'] = 0
    elif mutation == 'bad-slack': row['slack_ns'] = 'bad'
    elif mutation == 'boolean-normalized': row['normalized_slack'] = False
    elif mutation == 'invalid-cut': row['cut_nets'] = [False]
    write_json(path, timing)
    if mutation == 'duplicate-root': path.write_text('{"paths":[],"paths":[]}')
    elif mutation == 'truncated': path.write_text(path.read_text()[:-4])
    output = tmp_path / 'rejected'
    with geometry_case(case) as geometry:
        with pytest.raises(ValidationError):
            ChimewSignals(geometry, case[4], case[5], output, timing=path)
    assert not output.exists()
    assert not list(tmp_path.glob('.chimew-signals-*'))


@pytest.mark.parametrize('mutation', ['route-namespace', 'route-owner', 'route-seal',
    'late-schedule-generation', 'incomplete-geometry', 'existing-output'])
def test_native_signal_sources_and_publication_remain_bound(case, tmp_path, mutation):
    from emuflow.transport.chimew.signals import ChimewSignals
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    from tests.native_route_seal_fixture import write_route_store_fixture
    output = tmp_path / 'guarded-signals'
    with geometry_case(case) as geometry:
        if mutation == 'route-namespace':
            write_route_store_fixture(case[5], {'schema': 'emuflow.system-routes/v1',
                'design': 'foreign', 'platform': case[0].name, 'demands': [], 'routes': []})
        elif mutation in ('route-owner', 'route-seal'):
            manifest = json.loads(case[5].read_text())
            if mutation == 'route-owner': manifest['artifact'] = '../escape.sqlite'
            else: manifest['store_sha256'] = '0' * 64
            write_json(case[5], manifest)
        elif mutation == 'late-schedule-generation':
            case[4].write_text(case[4].read_text() + '\n')
        elif mutation == 'incomplete-geometry':
            # Re-seal a deliberately incomplete producer, so coverage must be
            # rejected independently of its byte checksum.
            with closing(sqlite3.connect(geometry.store_path)) as db, db:
                db.execute("DELETE FROM points WHERE seq=(SELECT min(seq) FROM points)")
            manifest = json.loads(geometry.header_path.read_text())
            manifest.update(store_bytes=geometry.store_path.stat().st_size,
                            store_sha256=file_sha256(geometry.store_path))
            write_json(geometry.header_path, manifest)
        else:
            output.mkdir(); (output / 'keep').write_text('previous generation')
        with pytest.raises(ValidationError):
            ChimewSignals(geometry, case[4], case[5], output)
        # These mutations deliberately invalidate this read lease; avoid its
        # expected exit error after asserting the producer's own rejection.
        geometry._generations.clear()
    if mutation == 'existing-output':
        assert (output / 'keep').read_text() == 'previous generation'
        assert sorted(p.name for p in output.iterdir()) == ['keep']
    else:
        assert not output.exists()
    assert not list(tmp_path.glob('.chimew-signals-*'))


@pytest.mark.parametrize('policy', ['plain', 'critical', 'near-one', 'split-direction', 'flat-y'])
def test_complete_native_lookahead_matches_retired_python_records(case, tmp_path, policy):
    from emuflow.transport.chimew.signals import ChimewSignals
    from emuflow.transport.chimew.chimew_lookahead import prepare_chimew_lookahead
    from emuflow.transport.chimew.chimew_bank_channel_storage import read_chimew_bank_channel_input
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    from tests.chimew_lookahead_reference import _materialize_xilinx_chimew_inputs
    platform, source, architecture, report, schedule_path, routes_path = case
    selected_entries(case, [{'id': 'zeta', 'demand': 'd-zeta', 'lane': 0, 'slot': 0, 'ready_slot': 0, 'arrival_slot': 1},
                            {'id': 'alpha', 'demand': 'd-alpha', 'lane': 0, 'slot': 1, 'ready_slot': 1, 'arrival_slot': 2}])
    if policy == 'split-direction':
        with closing(read_tdm_schedule(schedule_path)) as schedule:
            header = schedule.native_validation_header()
            entries = list(schedule['entries'])
        for allocation in header['bank_allocations']:
            forward = allocation['from'] == platform.links[0].endpoints[0]
            allocation.update(bank_count=9, lanes=180, lane_offset=0 if forward else 180)
        write_tdm_schedule_storage(schedule_path, {**header, 'entries': entries,
            'routes': [], 'domain_schedules': [], 'demand_completions': []})
    if policy == 'flat-y':
        from emuflow.common.native_tools import native_file_generation
        for record in report['fpgas']:
            stages = record['stages']
            if 'vivado_implementation' in stages:
                owner = stages['vivado_implementation']['cell_coverage']
                path = Path(owner['output'])
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE sites SET tile_row=4 WHERE stage='routed'")
                owner['sha256'] = file_sha256(path)
            else:
                owner = stages['rapidwright_implementation']['placement']['site_database']
                path = Path(owner['path'])
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('UPDATE sites SET physical_y=4')
                owner.update(sha256=file_sha256(path), generation=list(native_file_generation(path)))
    timing = None
    scale = 5e-10 if policy == 'near-one' else 9.0
    if policy in ('critical', 'near-one'):
        timing = tmp_path / 'projected.json'
        with closing(read_tdm_schedule(schedule_path)) as schedule:
            write_json(timing, {'schema': 'emuflow.sta-paths/v1', 'design': schedule['design'],
                'paths': [{'id': 'critical', 'clock_period_ns': 4.0, 'slack_ns': -2.0,
                           'cut_nets': ['n5']}]})
    with geometry_case(case) as geometry, ChimewSignals(geometry, schedule_path, routes_path,
            tmp_path / 'signals', timing=timing, weight_scale=scale, region_count=31) as signals:
        with closing(read_tdm_schedule(schedule_path)) as schedule:
            expected = _materialize_xilinx_chimew_inputs(ir_path=source,
                schedule_path=schedule_path, routes_path=routes_path,
                platform_path=research_platform_path(2), physical_report=report,
                geometry=geometry, signals=signals, source_schedule=schedule,
                timing_paths_path=timing, timing_weight_scale=scale, region_count=31,
                output_dir=tmp_path / 'reference')
        # The production producer must call the shared C++ models directly.
        with patch('tests.chimew_grouping_fixture.build_chimew_initial_groups',
                   side_effect=AssertionError('Python grouping producer was called')), patch(
                   'tests.chimew_refinement_fixture.refine_chimew_groups',
                   side_effect=AssertionError('Python refinement producer was called')):
            actual = prepare_chimew_lookahead(signals, tmp_path / 'native')
    assert actual['implementation_id'] == 'native-chimew-lookahead-preparation-v1'
    assert actual['metrics'] == expected['metrics']
    assert actual['timing_guard'] == expected['timing_guard']
    assert actual.get('timing_weighting') == expected.get('timing_weighting')
    for label in ('crossings', 'positions', 'rudy_input', 'electrical_map'):
        assert read_json(Path(actual['artifacts'][label]['path'])) == read_json(
            Path(expected['artifacts'][label]['path']))
    with read_chimew_bank_channel_input(Path(actual['artifacts']['bank_channel_input']['path'])) as lhs, \
         read_chimew_bank_channel_input(Path(expected['artifacts']['bank_channel_input']['path'])) as rhs:
        assert lhs.materialize() == rhs.materialize()
        assert lhs.logical_sha256 == rhs.logical_sha256
    assert file_sha256(Path(actual['sources']['placement'])) == file_sha256(
        Path(expected['sources']['placement']))
    # The downstream qualification still independently replays grouping and
    # refinement; a producer's expected replay digest cannot bypass that gate.
    artifacts = actual['artifacts']
    pipeline = run_chimew_phase6_pipeline(*[Path(artifacts[key]['path']) if key != 'platform'
        else research_platform_path(2) for key in ('schedule', 'platform', 'crossings',
            'positions', 'rudy_input', 'bank_channel_input', 'electrical_map')],
        tmp_path / 'qualified-pipeline', source_paths={k: Path(v) for k, v in actual['sources'].items()},
        region_count=31)
    assert pipeline['status'] == 'pass'


@pytest.mark.parametrize('mutation', ['signal-owner', 'signal-seal', 'schedule-identity',
    'crossing-mask', 'critical-flag', 'source-list', 'record-count', 'existing', 'symlink', 'dangling-symlink'])
def test_complete_native_lookahead_rejects_forged_inputs_and_preserves_output(case, tmp_path, mutation):
    from emuflow.transport.chimew.signals import ChimewSignals
    from emuflow.transport.chimew.chimew_lookahead import prepare_chimew_lookahead
    output = tmp_path / 'rejected-lookahead'
    previous = tmp_path / 'previous-generation'
    if mutation in ('existing', 'symlink'):
        previous.mkdir(); (previous / 'keep').write_text('previous')
        if mutation == 'existing': output = previous
        else: output.symlink_to(previous, target_is_directory=True)
    if mutation == 'dangling-symlink':
        output.symlink_to(previous, target_is_directory=True)
    pattern = 'symlink' if mutation in ('symlink', 'dangling-symlink') else 'native Chimew lookahead failed'
    with pytest.raises(ValidationError, match=pattern):
        with geometry_case(case) as geometry, ChimewSignals(geometry, case[4], case[5],
                tmp_path / 'signal-input') as signals:
            manifest = json.loads(signals.path.read_text())
            if mutation == 'signal-owner': manifest['artifact'] = '../foreign.sqlite'
            elif mutation == 'signal-seal': manifest['store_sha256'] = '0' * 64
            elif mutation not in ('existing', 'symlink', 'dangling-symlink'):
                with closing(sqlite3.connect(signals.store_path)) as db, db:
                    if mutation == 'schedule-identity': db.execute("UPDATE signals SET entry_id=entry_id||'-foreign'")
                    elif mutation == 'crossing-mask': db.execute('UPDATE signals SET encoding=encoding+1')
                    elif mutation == 'critical-flag': db.execute('UPDATE signals SET protected=2')
                    else:
                        header = manifest['header']
                        if mutation == 'source-list': header['input_artifacts'] = []
                        else: header['signals'] += 1
                        db.execute("UPDATE metadata SET value=? WHERE key='header'", (json.dumps(header),))
                manifest.update(store_bytes=signals.store_path.stat().st_size,
                                store_sha256=file_sha256(signals.store_path))
            write_json(signals.path, manifest)
            prepare_chimew_lookahead(signals, output)
    if mutation in ('existing', 'symlink'):
        assert (previous / 'keep').read_text() == 'previous'
        assert sorted(p.name for p in previous.iterdir()) == ['keep']
        if mutation == 'symlink': assert output.is_symlink()
    elif mutation == 'dangling-symlink':
        assert output.is_symlink() and not previous.exists()
    else:
        assert not output.exists()
    assert not list(tmp_path.glob('.chimew-lookahead-*'))
