"""Differential contracts for native model generation; NVDLA remains acceptance."""
from contextlib import closing
import json
import sqlite3
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from unittest.mock import patch
import pytest

from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow_tools.stage_clients.opensta import OPENSTA_PROVIDER
from emuflow.partition.partition import (CUT_MODE_STATIC_EXACT)
from tests.native_partition_facts import (build_clusters, build_partition_assignment)
from emuflow_tools.partition.partition_ir import load_partition_ir
from tests.patron_kernel_fixture import pressure_kernel_inputs, write_pressure_requests, run_pressure_kernel
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.timing.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming
from tests.native_build import emuflow_native, patron_ingress_oracle, hop_partition_refiner
from tests import partition_pressure_fixture as reference_fixture


@pytest.fixture(params=['normal','reordered','duplicate-sinks','sparse','balanced'])
def inputs(tmp_path, monkeypatch,request):
    native=tmp_path/"native"; (native/"bin").mkdir(parents=True)
    for name,binary in (("emuflow-native",emuflow_native()),):
        (native/"bin"/name).symlink_to(binary)
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(native))
    fixture=reference_fixture.PressureFixture(); fixture.setUp()
    if request.param=='reordered':
        fixture.ir.value['instances'].reverse()
        fixture.ir.value['nets'].reverse()
    if request.param=='duplicate-sinks':
        fixture.ir.value['nets'][0]['sinks'].append({'instance':'u1','port':'I_extra','bit':0})
    if request.param=='balanced':
        fixture.constraints['balance_tolerance']=0.0
        fixture.constraints['balance_dimensions']=['cells','lut']
        fixture.constraints['balance_tolerance_by_dimension']={'lut':0.125}
    raw=tmp_path/'ir.json'; write_json(raw,fixture.ir.value)
    compact=tmp_path/'ir.emuir.sqlite'; write_compact_emuir(compact,fixture.ir.value)
    if request.param=='sparse':
        with closing(sqlite3.connect(compact)) as db,db:
            db.execute('UPDATE endpoint_records SET ordinal=ordinal*7+5')
    ir=load_partition_ir(compact)
    clusters=build_clusters(ir,fixture.constraints,cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    initial=build_partition_assignment(ir,fixture.platform,clusters,fixture.constraints,
        {f'c{i:06d}':('a' if i%2==0 else 'b') for i in range(len(clusters['clusters']))},'fixture',7,
        _include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    rows=[STA_PATH_DATABASE_TSV_HEADER]
    for i,path in enumerate(fixture.timing['paths']):
        identity=f"{path['startpoint']['instance']}/O->{path['endpoint']['instance']}/I#{i:08d}"
        rows.append('\t'.join([identity.encode().hex(),'clk'.encode().hex(),str(path['clock_period_ns']),str(path['slack_ns']),
            str(path['fixed_delay_ns']),','.join(n.encode().hex() for n in path['path_nets'])]))
    tsv=tmp_path/'paths.tsv'; tsv.write_text('\n'.join(rows)+'\n')
    timing=tmp_path/'timing.json'; import_sta_path_database_tsv_streaming(tsv,compact,timing,provider=OPENSTA_PROVIDER)
    manifest=read_json(timing)
    args=(ir,fixture.platform,clusters,fixture.constraints,manifest,fixture.route_constraints)
    return args,initial,timing


def run(args,model,initial,max_moves=0):
    return run_pressure_kernel(model,initial, max_moves=max_moves)


def test_production_result_borrows_readonly_native_ownership_without_host_cluster_parsing(inputs):
    import gc
    from tests.partition_views import ClusterOwners as _PackedClusterAssignment
    args,initial,timing=inputs
    initial=dict(initial,cluster_assignment=_PackedClusterAssignment(('a','b'),
        (0 if initial['cluster_assignment'][f'c{i:06d}']=='a' else 1 for i in range(len(args[2]['clusters'])))))
    model=pressure_kernel_inputs(*args,timing_database_path=timing)
    with patch.object(_PackedClusterAssignment, '__init__', side_effect=AssertionError('host part array')):
        actual,trace=run(args,model,initial,max_moves=0)
    owners=actual['cluster_assignment']
    root=owners.native_output_source.path.parent
    assert owners.part_indices.readonly
    assert owners==initial['cluster_assignment']
    with pytest.raises(TypeError):owners.part_indices[0]=1
    assert owners.native_output_source.path.is_file()
    assert owners.native_order_source.path.is_file()
    assert not (root/'initial.bin').exists()
    assert not (root/'board.bin').exists()
    del owners,actual,trace
    gc.collect()
    assert not root.exists()


def test_result_requires_the_original_producer_generation(inputs):
    from tests.native_partition_core_fixture import audit_assignment
    args, initial, timing = inputs
    model = pressure_kernel_inputs(*args, timing_database_path=timing)
    actual, _ = run(args, model, initial, max_moves=0)
    output = actual['cluster_assignment'].native_output_source.path
    output.write_bytes(output.read_bytes())
    with pytest.raises(Exception, match='generation'):
        audit_assignment(args[1], args[2], actual)


@pytest.mark.parametrize('max_moves',[0,2])
def test_primary_model_records_match_fixed_source_facts_and_borrow_primary(inputs,tmp_path,max_moves):
    args,initial,timing=inputs
    ir,platform,clusters,constraints,manifest,routes=args
    model=pressure_kernel_inputs(*args,timing_database_path=timing)
    protected=(clusters['clusters']._member_offsets,clusters['clusters'].source_cluster_indices,
               *clusters['clusters']._resource_columns)
    original=__import__('emuflow.common.native_protocol',fromlist=['CheckedRequestWriter']).CheckedRequestWriter.bytes
    def bounded_write(writer,value):
        assert all(value is not column for column in protected),'duplicated primary column'
        return original(writer,value)
    with patch('emuflow.common.native_protocol.CheckedRequestWriter.bytes',bounded_write):
        columns,board=write_pressure_requests(model,tmp_path,initial,max_moves)
        assert columns.stat().st_size==40+4*len(clusters['clusters'])+8*len(clusters['clusters']._fixed_by_cluster)
        result=subprocess.run([str(patron_ingress_oracle()),'--facts',str(ir.path),str(model.timing_path),
            str(model.primary_artifact),str(columns),str(board)],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        facts=json.loads(result.stdout)
        actual,trace=run(args,model,initial,max_moves)
    by_instance={member:index for index,cluster in enumerate(clusters['clusters']) for member in cluster['instances']}
    assert facts['clusters']==4 and len(facts['nets'])==2 and len(facts['paths'])==2
    expected={((by_instance['u0'],),(by_instance['u1'],)), ((by_instance['u2'],),(by_instance['u3'],))}
    assert {(tuple(row['drivers']),tuple(row['sinks'])) for row in facts['nets']}==expected
    assert sorted((path['period_ns'],path['slack_ns']) for path in facts['paths'])==[(10.0,3.0),(20.0,20.0)]
    for path in facts['paths']:
        assert len(path['nets'])==1
        net=facts['nets'][path['nets'][0]]
        assert net['drivers']==[path['start_cluster']]
        assert path['end_cluster'] in net['sinks']
    assert trace['native_input_transport']=='native-primary-store-model-v2'
    assert trace['model_counts']=={'clusters':4,'nets':2,'paths':2,'capacity_domains':len(model.domains)}
    from tests.native_partition_core_fixture import audit_assignment
    assert audit_assignment(platform,clusters,actual).require(platform,actual)['clusters']==4



def test_actual_patron_writer_applies_nonidentity_publication_coordinates(inputs,tmp_path):
    args,initial,timing=inputs
    model=pressure_kernel_inputs(*args,timing_database_path=timing)
    columns,board=write_pressure_requests(model,tmp_path,initial,0)
    output=tmp_path/'output.bin'
    result=subprocess.run([str(patron_ingress_oracle()),'--publish-order-from-primary',str(args[0].path),
        str(model.timing_path),str(model.primary_artifact),str(columns),str(board),str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    payload=output.read_bytes()
    assert zlib.crc32(payload[:-4])==struct.unpack('<I',payload[-4:])[0]
    ids=sorted(cluster['id'] for cluster in args[2]['clusters']);parts=sorted(args[3]['active_fpgas'])
    expected=[parts.index(initial['cluster_assignment'][identity]) for identity in reversed(ids)]
    assert list(struct.unpack('<'+str(len(ids))+'I',payload[-4-4*len(ids):-4]))==expected



@pytest.mark.parametrize('corruption',['checksum','membership','owner','source','version','truncated','totals','source-counts'])
def test_primary_owner_rejects_corruption_before_publication(inputs,tmp_path,corruption):
    args,initial,timing=inputs
    clusters=build_clusters(args[0],args[3],cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    direct_args=(*args[:2],clusters,*args[3:])
    model=pressure_kernel_inputs(*direct_args,timing_database_path=timing)
    copy=tmp_path/'untrusted-primary.bin'
    shutil.copyfile(model.primary_artifact,copy)
    data=bytearray(copy.read_bytes())
    cells,count=struct.unpack_from('<QQ',data,12)
    offsets=200+8*(cells+1)
    if corruption=='checksum': data[-1]^=1
    elif corruption=='truncated': del data[-1]
    else:
        if corruption=='membership': struct.pack_into('<I',data,offsets+4,0)
        elif corruption=='owner': struct.pack_into('<I',data,offsets+4*(count+1)+4*cells,count)
        elif corruption=='source': data[44]^=1
        elif corruption=='version': struct.pack_into('<I',data,8,1)
        elif corruption=='totals': struct.pack_into('<Q',data,100,999)
        elif corruption=='source-counts': struct.pack_into('<Q',data,12,2**32-1)
        struct.pack_into('<I',data,196,zlib.crc32(data[:196]))
        struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
    copy.write_bytes(data)
    model.primary_artifact=copy
    model.primary_generation=native_file_generation(copy)
    # The malformed primary is an explicit untrusted input. Encode the same
    # small ownership facts without borrowing a different primary's receipt;
    # the native reader must reject the corrupted bytes itself.
    untrusted_initial=dict(initial,cluster_assignment=dict(initial['cluster_assignment']))
    columns,board=write_pressure_requests(model,tmp_path,untrusted_initial,0)
    output=tmp_path/'result.bin'; output.write_bytes(b'keep-published-result')
    result=subprocess.run([str(emuflow_native()),'phase3','refine-patron-from-primary',str(args[0].path),
        str(model.timing_path),str(copy),str(columns),str(board),str(output)],capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_bytes()==b'keep-published-result'
    assert not Path(str(output)+'.summary.json').exists()


def test_selected_primary_owner_does_not_fall_back_when_reference_is_lost(inputs,tmp_path):
    from emuflow.common.errors import ValidationError
    args,initial,timing=inputs
    clusters=build_clusters(args[0],args[3],cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    direct_args=(*args[:2],clusters,*args[3:])
    model=pressure_kernel_inputs(*direct_args,timing_database_path=timing)
    model.primary_artifact=None
    with pytest.raises(ValidationError,match='selected direct primary ownership is unavailable'):
        run(direct_args,model,initial)
    clusters['clusters'].native_artifact=None
    with pytest.raises(ValidationError,match='native primary artifact reference is unavailable'):
        pressure_kernel_inputs(*direct_args,timing_database_path=timing)


@pytest.mark.parametrize('max_moves',[0,2])
def test_native_model_is_deterministic_without_python_graph_or_protocol(inputs,max_moves):
    args,initial,timing=inputs
    a=pressure_kernel_inputs(*args,timing_database_path=timing)
    b=pressure_kernel_inputs(*args,timing_database_path=timing)
    with patch('emuflow_tools.partition.partition_ir._CompactPartitionInstances.__iter__',side_effect=AssertionError('host graph')):
        actual,trace=run(args,a,initial,max_moves)
        repeated,other=run(args,b,initial,max_moves)
    assert dict(actual['cluster_assignment'])==dict(repeated['cluster_assignment'])
    assert trace['initial_metrics']==other['initial_metrics']
    assert trace['final_metrics']==other['final_metrics']
    assert trace['model_counts']==other['model_counts']
    assert trace['change_count']==other['change_count']
    for value in trace['native_model_metrics'].values():
        assert value['wall_seconds']>0 and value['rss_scope']=='self-lifetime-high-water'



def test_native_model_summary_schema(inputs,tmp_path):
    jsonschema=pytest.importorskip('jsonschema')
    from referencing import Registry,Resource
    args,initial,timing=inputs
    model=pressure_kernel_inputs(*args,timing_database_path=timing)
    columns,board=write_pressure_requests(model,tmp_path,initial,0)
    output=tmp_path/'result'
    result=subprocess.run([str(emuflow_native()),'phase3','refine-patron-from-primary',str(args[0].path),str(model.timing_path),str(model.primary_artifact),str(columns),str(board),str(output)],
                          capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    schemas=Path(__file__).resolve().parents[1]/'schemas'
    operation=schemas/'native-stage-result-v1.schema.json'
    registry=Registry().with_resource(operation.as_uri(),Resource.from_contents(json.loads(operation.read_text())))
    path=schemas/'patron-native-model-v1.schema.json'
    jsonschema.Draft202012Validator(dict(json.loads(path.read_text()),**{'$id':path.as_uri()}),registry=registry).validate(
        json.loads(Path(str(output)+'.summary.json').read_text()))


@pytest.mark.parametrize("explicit_unified", [False, True])
def test_phase3_production_selects_native_model_and_keeps_independent_validation(inputs,tmp_path,explicit_unified):
    from emuflow_tools.stage_clients.partition import run_phase3
    args,initial,timing=inputs
    platform=tmp_path/'platform.json'; write_json(platform,args[1].to_dict())
    routes=tmp_path/'routes.json'; write_json(routes,args[5])
    solution=tmp_path/'initial.part.2'
    solution.write_text('\n'.join('0' if initial['cluster_assignment'][c['id']]=='a' else '1'
                                   for c in args[2]['clusters'])+'\n')
    with patch('tests.native_partition_facts.build_partition_assignment', side_effect=AssertionError('Python initializer')):
        report=run_phase3(args[0].path,platform,tmp_path/'phase3',provider='patron',
                         min_used_fpgas=2,balance_tolerance=1.0,patron_max_moves=0,
                         tritonpart_solution=solution,route_constraints_path=routes,timing_database_path=timing,
                         hop_refiner=str(hop_partition_refiner()),
                         patron_refiner=str(emuflow_native()) if explicit_unified else None)
    assert report['status']=='pass'
    assert report['cluster_implementation_id']=='native-primary-clusters-v1'
    assert report['candidate_implementation_id']=='native-candidate-ids-v1'
    assert report['semantic_implementation_id']=='native-selected-partition-semantics-v1'
    assert report['assignment_descriptor_writer_implementation_id']=='native-indexed-assignment-descriptor-v1'
    assert report['native_assignment_index_metrics']['host_ownership_records']==0
    assert report['stage_implementation_id']=='native-phase3-v1'
    assert report['algorithm_validation']['model']['implementation_id']=='native-primary-store-model-v2'
    assert report['algorithm_validation']['model']['nets']==2
    assert report['algorithm_validation']['model']['model_build']['metrics']['wall_seconds']>0
    runtime=__import__('json').loads((tmp_path/'phase3/phase3-runtime.json').read_text())['native_stage']
    stages={step['stage']:step for step in runtime['steps']}
    for name in ('phase3.clusters','phase3.candidates','phase3.patron','phase3.decision','phase3.publish-partition'):
        assert stages[name]['status']=='pass'
        assert stages[name]['metrics']['wall_seconds']>0
    assert report['algorithm_validation']['candidate_selection']['initial_provider']=='tritonpart-openroad-hypergraph-v1'


@pytest.mark.parametrize('corruption',['checksum','owner','source','path-order'])
def test_invalid_sources_are_rejected_without_publishing(inputs,tmp_path,corruption):
    args,initial,timing=inputs
    model=pressure_kernel_inputs(*args,timing_database_path=timing)
    columns,board=write_pressure_requests(model,tmp_path,initial,0)
    if corruption in ('checksum','owner'):
        data=bytearray(columns.read_bytes())
        if corruption=='checksum': data[-1]^=1
        else:
            # The first uint32 after the header is an initial FPGA index.
            struct.pack_into('<I',data,28,len(model.partition_fpgas))
            struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
        columns.write_bytes(data)
        # Rebind only the columns generation in the private board request so
        # the malformed payload, rather than stale-reference check, is tested.
        from emuflow.common.native_tools import native_file_generation
        request=bytearray(board.read_bytes())
        struct.pack_into('<3Q4q',request,12+3*56,*native_file_generation(columns))
        struct.pack_into('<I',request,len(request)-4,zlib.crc32(request[:-4]))
        board.write_bytes(request)
    elif corruption=='source':
        with closing(sqlite3.connect(args[0].path)) as db,db:
            db.execute("UPDATE net_records SET cut_class='invalid' WHERE seq=0")
    else:
        with closing(sqlite3.connect(model.timing_path)) as db,db:
            db.execute('UPDATE path_nets SET net_ordinal=7 WHERE path_ordinal=0')
        from emuflow.common.native_tools import native_file_generation
        request=bytearray(board.read_bytes())
        struct.pack_into('<3Q4q',request,12+56,*native_file_generation(model.timing_path))
        struct.pack_into('<I',request,len(request)-4,zlib.crc32(request[:-4]))
        board.write_bytes(request)
    output=tmp_path/'result'; output.write_text('previous')
    command=[str(emuflow_native()),'phase3','refine-patron-from-primary',str(args[0].path),str(model.timing_path),str(model.primary_artifact),str(columns),str(board),str(output)]
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_text()=='previous'
    assert not Path(str(output)+'.order').exists()
    assert not Path(str(output)+'.summary.json').exists()
    assert not list(tmp_path.glob('*.stage.*'))


def test_retired_snapshot_command_is_rejected_before_opening_inputs(tmp_path):
    output=tmp_path/'published';output.write_bytes(b'published-result')
    result=subprocess.run([str(emuflow_native()),'phase3','refine-patron-from-store',
        str(tmp_path/'missing-ir'),str(tmp_path/'missing-timing'),
        str(tmp_path/'missing-columns'),str(tmp_path/'missing-request'),str(output)],
        capture_output=True,text=True)
    assert result.returncode==2
    assert output.read_bytes()==b'published-result'
    assert not Path(str(output)+'.order').exists()
    assert not Path(str(output)+'.summary.json').exists()


def test_native_pressure_rejects_reference_columns_instead_of_snapshot(inputs):
    from emuflow.common.errors import ValidationError
    args,_initial,timing=inputs
    # Plain explicit facts carry no native primary owner; no retired builder
    # is needed to exercise this required rejection.
    reference=dict(args[2],clusters=list(args[2]['clusters']))
    with pytest.raises(ValidationError,match='native primary artifact reference is unavailable'):
        pressure_kernel_inputs(*args[:2],reference,*args[3:],
            timing_database_path=timing)


@pytest.mark.parametrize('damage', ['empty', 'truncated', 'version', 'checksum', 'part-domain'])
def test_current_typed_result_rejects_resealed_malformed_payload(inputs, damage):
    from emuflow.common.errors import ValidationError
    from emuflow.storage.store_publication import bind_store_source
    from tests.native_partition_core_fixture import audit_assignment
    args, initial, timing = inputs
    model = pressure_kernel_inputs(*args, timing_database_path=timing)
    actual, _ = run(args, model, initial, max_moves=0)
    owner = actual['cluster_assignment']
    output = owner.native_output_source.path
    data = bytearray(output.read_bytes())
    if damage == 'empty': data.clear()
    elif damage == 'truncated': del data[30:]
    elif damage == 'version': struct.pack_into('<I', data, 8, 99)
    elif damage == 'checksum': data[-1] ^= 1
    else:
        struct.pack_into('<I', data, len(data)-4-4*len(owner), len(owner.part_ids))
        struct.pack_into('<I', data, len(data)-4, zlib.crc32(data[:-4]))
    output.write_bytes(data)
    owner.native_output_source = bind_store_source(output, 'patron-output-v1')
    # The maintained native consumer, rather than a retired Python decoder,
    # must reject malformed optimizer output even after generation rebinding.
    with pytest.raises(ValidationError):
        audit_assignment(args[1], args[2], actual)
