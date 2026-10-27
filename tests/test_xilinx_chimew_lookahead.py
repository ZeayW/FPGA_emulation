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
from emuflow.transport.chimew.chimew_lookahead import materialize_xilinx_chimew_inputs, _fixed_channels
from emuflow.transport.chimew.chimew_pipeline import run_chimew_phase6_pipeline
from emuflow.platform.default_platform import research_platform_path
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json, file_sha256

from emuflow.physical.vivado.vivado_lineage import import_cell_lineage, PLACEMENT_COORDINATE_SYSTEM
from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxChimewGeometry, read_xilinx_package_geometry
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
            from emuflow.physical.openparf.xilinx_openparf_atomic import _write_site_database
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
    routes = tmp_path/'routes.json'; write_json(routes, {'routes':[]})
    return platform, source, architecture, {'fpgas':records}, schedule_path, routes


def test_selected_native_locations_and_full_paper_pipeline(case, tmp_path):
    platform, source, architecture, report, schedule, routes = case
    initial_schedule_hash = file_sha256(schedule)
    with XilinxChimewGeometry(source, platform, report, architecture) as geometry:
        for fpga, kind in ((platform.fpgas[0].id, 'tx'), (platform.fpgas[1].id, 'rx')):
            point = geometry.point(fpga, 'n5', 's0', kind)
            assert all(isinstance(v, float) for v in point)
            assert 0 <= point[2] <= 1
            if "rapidwright_implementation" in report["fpgas"][0]["stages"]:
                index = 0 if kind == "tx" else 1
                assert point[:2] == (3+index*10,4+index*10)
        assert geometry.forwarded == 0
        # No materialized location view / instance map hides a per-entry scan.
        for db in geometry.databases.values():
            assert db.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
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
        XilinxChimewGeometry(source,platform,report,architecture)


def test_missing_active_placement_is_not_a_synthetic_centre(case):
    platform, source, architecture, report, *_=case
    with XilinxChimewGeometry(source,platform,report,architecture) as geometry:
        with pytest.raises(ValidationError,match='lacks placed source or boundary'):
            geometry.point(platform.fpgas[0].id,'n7','s0','rx')


def test_generation_mutation_is_rejected(case):
    platform, source, architecture, report, *_=case
    with pytest.raises(ValidationError,match='generation changed'):
        with XilinxChimewGeometry(source,platform,report,architecture):
            architecture.write_text(architecture.read_text()+'\n')


def test_wrong_mapped_generation_seal_is_rejected(case):
    platform,source,architecture,report,*_=case
    stages=report['fpgas'][0]['stages']
    if 'mapped_verilog' in stages:
        stages['mapped_verilog']['mapped_index']['sha256']='0'*64
    else:
        stages['rapidwright_implementation']['result']['artifacts']['mapped']['sha256']='0'*64
    with pytest.raises(ValidationError,match='placement/source identity'):
        XilinxChimewGeometry(source,platform,report,architecture)


def test_timing_guard_has_one_owner_without_cloning_the_hop_store(case, tmp_path):
    from emuflow.transport.chimew.chimew_grouping import build_chimew_initial_groups
    from emuflow.transport.chimew.chimew_refinement import refine_chimew_groups
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


def test_prepass_and_final_share_the_selected_backend_options(tmp_path):
    import inspect
    from types import SimpleNamespace
    import emuflow.flow.multi_fpga_flow as flow
    defaults={k:p.default for k,p in inspect.signature(flow.run_multi_fpga_flow).parameters.items()
              if p.default is not inspect.Parameter.empty}
    for backend in ('vivado','rapidwright'):
        ctx=SimpleNamespace(**defaults)
        ctx.physical_backend=backend
        ctx.effective_physical_architecture=tmp_path/'architecture.json'
        ctx.ir_path=tmp_path/'source.sqlite';ctx.assignment_path=tmp_path/'assignment.json'
        ctx.routes_path=tmp_path/'routes.json';ctx.path_database_path=tmp_path/'paths.json'
        ctx.physical_seed=1;ctx.physical_workers=4
        ctx.measured=lambda stage,fn,*args,**kwargs:fn(*args,**kwargs)
        with patch.object(flow,'run_multi_fpga_physical_flow') as run:
            flow._run_selected_physical(ctx,tmp_path/'baseline',tmp_path/'schedule',tmp_path/'physical0')
            flow._run_selected_physical(ctx,tmp_path/'candidate',tmp_path/'schedule',tmp_path/'physical1')
        before,after=run.call_args_list
        assert before.kwargs == after.kwargs
        assert before.kwargs['backend']==backend
        assert before.kwargs['seed']==1 and before.kwargs['workers']==4


def test_chimew_preflight_accepts_both_xilinx_backends_before_frontend(case, tmp_path):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    platform,source,architecture,report,*_=case
    class PreflightReached(Exception): pass
    for backend in ('rapidwright','vivado'):
        out=tmp_path/f'preflight-{backend}'
        with patch('emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract',side_effect=PreflightReached):
            with pytest.raises(PreflightReached):
                run_multi_fpga_flow(platform_path=research_platform_path(2),output_dir=out,
                    phase6_provider='chimew',physical=True,physical_backend=backend,
                    physical_architecture=architecture)
        assert not out.exists()


def test_multicast_query_cache_is_bounded_and_uses_indexes(case):
    platform,source,architecture,report,*_=case
    with closing(sqlite3.connect(source)) as db,db:
        seq=db.execute("SELECT seq FROM net_records WHERE id='n5'").fetchone()[0]
        # Repeated independent net records exercise the design-sized query count.
        first=db.execute('SELECT max(seq)+1 FROM net_records').fetchone()[0]
        for index in range(4100):
            db.execute('INSERT INTO net_records SELECT ?,?,name,flags,yosys_bit,bus_index,fanout,cut_class,extras_seq FROM net_records WHERE seq=?',
                       (first+index,f'repeated-{index}',seq))
            db.execute('INSERT INTO endpoint_records SELECT ?,role,ordinal,instance_seq,port_seq,bit,extras_seq FROM endpoint_records WHERE net_seq=?',
                       (first+index,seq))
    with XilinxChimewGeometry(source,platform,report,architecture) as geometry:
        fpga=platform.fpgas[0].id
        for index in range(4100):
            geometry.point(fpga,f'repeated-{index}','s0','tx')
        assert len(geometry.source_points)==4096
        expr=geometry.location_sql[fpga].format(axis='column' if 'tile_' in geometry.location_sql[fpga] else 'x',instance="'launch'")
        plan=' '.join(row[3] for row in geometry.databases[fpga].execute('EXPLAIN QUERY PLAN '+expr))
        assert 'SCAN cell' not in plan and 'SCAN a' not in plan
        if 'tile_' not in geometry.location_sql[fpga]:
            assert 'physical_sites_name' in plan


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
    fpga=record['fpga']
    with XilinxChimewGeometry(source,platform,report,architecture) as geometry:
        if len(registers)==1:
            expected=geometry.point(fpga,'n5','original','rx')
            assert geometry.point(fpga,'n5','relay','tx')==expected
            assert geometry.forwarded==1
        else:
            with pytest.raises(ValidationError,match='boundary coverage'):
                geometry.point(fpga,'n5','relay','tx')
        with pytest.raises(ValidationError,match='boundary coverage'):
            geometry.point(fpga,'wrong-net','relay','tx')


def test_preflight_catalog_is_reused_without_rescanning_the_device_graph(case):
    from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
    platform,source,architecture,report,*_=case
    catalog=XilinxPackageGeometry.open(architecture,platform.fpgas[0].part)
    with patch('emuflow.transport.chimew.xilinx_chimew_geometry.read_xilinx_package_geometry',side_effect=AssertionError('device graph reread')):
        with XilinxChimewGeometry(source,platform,report,architecture,package_geometry=catalog) as geometry:
            geometry.point(platform.fpgas[0].id,'n5','s0','tx')


@pytest.mark.parametrize("case",["rapidwright"],indirect=True)
def test_shared_site_map_seal_is_published_once_and_survives_later_hardlinks(case, tmp_path):
    import os
    from emuflow.common.native_tools import native_file_generation
    from emuflow.physical.openparf.xilinx_openparf_atomic import _materialize_site_database
    platform,source,architecture,report,*_=case
    device={'coordinate_system':{'coordinate_system':PLACEMENT_COORDINATE_SYSTEM,
        'architecture_sha256':file_sha256(architecture),'sites':[
            {'dense_x':index,'dense_y':0,'physical_x':3+index*10,'physical_y':4+index*10,
             'placement_x':9999,'placement_y':8888,'site':f'S{index}',
             'resources':{'FF':16},'physical_sites':{'FF':[f'S{index}']}} for index in range(2)]}}
    with patch('emuflow.physical.openparf.xilinx_openparf_atomic._sha256',wraps=file_sha256) as producer_hash:
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
    reads=[]
    def checked_hash(path):
        if Path(path).samefile(first): reads.append(path)
        return file_sha256(path)
    with patch('emuflow.transport.chimew.xilinx_chimew_geometry.file_sha256',side_effect=checked_hash):
        with XilinxChimewGeometry(source,platform,report,architecture) as geometry:
            assert geometry.point(platform.fpgas[0].id,'n5','s0','tx')[:2]==(3,4)
            assert geometry.point(platform.fpgas[1].id,'n5','s0','rx')[:2]==(13,14)
    assert len(reads)==1


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
        XilinxChimewGeometry(source,platform,report,architecture)


@pytest.mark.parametrize('case',['rapidwright'],indirect=True)
def test_site_map_publisher_rejects_a_changed_shared_generation(case, tmp_path):
    import os
    from emuflow.physical.openparf.xilinx_openparf_atomic import _materialize_site_database
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
