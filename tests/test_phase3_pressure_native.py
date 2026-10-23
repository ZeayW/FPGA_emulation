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

from emuflow.emuir_compact import write_compact_emuir
from emuflow.io import read_json, write_json
from emuflow.opensta import OPENSTA_PROVIDER
from emuflow.partition import build_clusters, build_partition_assignment, CUT_MODE_STATIC_EXACT
from emuflow.partition_ir import load_partition_ir
from emuflow.partition_pressure import (build_partition_pressure_model, run_partition_pressure_native,
    _write_patron_native_input, _NativeBinaryPipeWriter)
from emuflow.phase3_pressure_native import NativePressureModel
from emuflow.native_tools import native_file_generation
from emuflow.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming
from tests.native_build import emuflow_native, patron_ingress_oracle, hop_partition_refiner, patron_refiner
from tests import test_partition_pressure as reference_fixture


@pytest.fixture(params=['normal','reordered','duplicate-sinks','sparse','balanced'])
def inputs(tmp_path, monkeypatch,request):
    native=tmp_path/"native"; (native/"bin").mkdir(parents=True)
    for name,binary in (("emuflow-native",emuflow_native()),("emuflow_patron_refiner",patron_refiner())):
        (native/"bin"/name).symlink_to(binary)
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(native))
    fixture=reference_fixture.PartitionPressureTest(); fixture.setUp()
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
    clusters=build_clusters(ir,fixture.constraints,cut_mode=CUT_MODE_STATIC_EXACT)
    initial=build_partition_assignment(ir,fixture.platform,clusters,fixture.constraints,
        {f'c{i:06d}':('a' if i%2==0 else 'b') for i in range(len(clusters['clusters']))},'fixture',7,
        _include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    rows=[STA_PATH_DATABASE_TSV_HEADER]
    for i,path in enumerate(fixture.timing['paths']):
        identity=f"{path['startpoint']['instance']}/O->{path['endpoint']['instance']}/I#{i:08d}"
        rows.append('\t'.join([identity.encode().hex(),'clk'.encode().hex(),str(path['clock_period_ns']),str(path['slack_ns']),
            str(path['fixed_delay_ns']),','.join(n.encode().hex() for n in path['path_nets'])]))
    tsv=tmp_path/'paths.tsv'; tsv.write_text('\n'.join(rows)+'\n')
    timing=tmp_path/'timing.json'; import_sta_path_database_tsv_streaming(tsv,raw,timing,provider=OPENSTA_PROVIDER)
    manifest=read_json(timing)
    args=(ir,fixture.platform,clusters,fixture.constraints,manifest,fixture.route_constraints)
    return args,initial,timing


def run(args,model,initial,max_moves=0):
    return run_partition_pressure_native(*args[:4],args[5],model,initial,algorithm_version=14,
        max_moves=max_moves,retain_trace_seals=False,stream_semantic_records=True,output_validation='caller',
        _release_model_before_assignment=True,_defer_candidate_derived_records=True)


@pytest.mark.parametrize('max_moves',[0,2])
def test_primary_owner_direct_model_matches_every_record_and_search(inputs,tmp_path,max_moves):
    args,initial,timing=inputs
    ir,platform,_clusters,constraints,manifest,routes=args
    clusters=build_clusters(ir,constraints,cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    direct_args=(ir,platform,clusters,constraints,manifest,routes)
    reference=build_partition_pressure_model(*direct_args,independent_validation=False,timing_database_path=timing)
    expected=tmp_path/'expected.bin'
    _write_patron_native_input(None,platform,clusters,constraints,routes,reference,initial,max_moves,14,
                              _writer=_NativeBinaryPipeWriter(expected.open('wb')))
    expected_assignment,expected_trace=run(direct_args,reference,initial,max_moves)
    model=build_partition_pressure_model(*direct_args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    assert model.implementation_id=='native-primary-store-model-v2'
    protected=(clusters['clusters']._member_offsets,clusters['clusters'].source_cluster_indices,
               *clusters['clusters']._resource_columns)
    original=__import__('emuflow.phase3_pressure_native',fromlist=['_Writer'])._Writer.bytes
    def bounded_write(writer,value):
        assert all(value is not column for column in protected),'duplicated primary column'
        return original(writer,value)
    with patch('emuflow.phase3_pressure_native._Writer.bytes',bounded_write):
        columns,board=model.write_requests(tmp_path,initial,max_moves)
        assert columns.stat().st_size==40+4*len(clusters['clusters'])+8*len(clusters['clusters']._fixed_by_cluster)
        result=subprocess.run([str(patron_ingress_oracle()),str(ir.path),str(model.timing_path),
            str(model.primary_artifact),str(columns),str(board),str(expected)],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        actual,trace=run(direct_args,model,initial,max_moves)
    assert dict(actual['cluster_assignment'])==dict(expected_assignment['cluster_assignment'])
    assert trace['initial_metrics']==expected_trace['initial_metrics']
    assert trace['final_metrics']==expected_trace['final_metrics']
    assert trace['native_input_transport']=='native-primary-store-model-v2'
    assert trace['change_count']==expected_trace['change_count']


@pytest.mark.parametrize('corruption',['checksum','membership','owner','source','version','truncated','totals','source-counts'])
def test_primary_owner_rejects_corruption_before_publication(inputs,tmp_path,corruption):
    args,initial,timing=inputs
    clusters=build_clusters(args[0],args[3],cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    direct_args=(*args[:2],clusters,*args[3:])
    model=build_partition_pressure_model(*direct_args,independent_validation=False,
                                       timing_database_path=timing,native_store_model=True)
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
    columns,board=model.write_requests(tmp_path,initial,0)
    output=tmp_path/'result.bin'; output.write_bytes(b'keep-published-result')
    result=subprocess.run([str(emuflow_native()),'phase3','refine-patron-from-primary',str(args[0].path),
        str(model.timing_path),str(copy),str(columns),str(board),str(output)],capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_bytes()==b'keep-published-result'
    assert not Path(str(output)+'.summary.json').exists()


def test_offline_primary_handoff_tool_compares_complete_models_and_retires_payloads(inputs,tmp_path):
    args,_initial,timing=inputs
    platform=tmp_path/'platform.json'; write_json(platform,args[1].to_dict())
    output=tmp_path/'qualification'
    script=Path(__file__).resolve().parents[1]/'scripts/validation/qualify_primary_store_handoff.py'
    import sys
    result=subprocess.run([sys.executable,str(script),'--ir',str(args[0].path),'--platform',str(platform),
        '--timing-database',str(timing),'--oracle',str(patron_ingress_oracle()),'--out',str(output),
        '--expected-design',args[0].value['design']['name']],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    summary=read_json(output/'qualification.json')
    assert summary['status']=='pass'
    assert summary['arms']['direct']['signature']==summary['arms']['snapshot']['signature']
    assert summary['arms']['direct']['emitted_columns_bytes']<summary['arms']['snapshot']['emitted_columns_bytes']
    assert not (output/'tmp').exists()
    assert list(output.iterdir())==[output/'qualification.json']
    fake=tmp_path/'fixture-openroad'
    fake.write_text('#!'+sys.executable+'\n'+'''import re,sys
from pathlib import Path
tcl=Path(sys.argv[-1]).read_text()
graph=Path(re.search(r'-hypergraph_file \\{(.*?)\\}',tcl).group(1))
parts=int(re.search(r'-num_parts (\\d+)',tcl).group(1))
with graph.open() as stream: count=int(stream.readline().split()[1])
Path(str(graph)+'.part.'+str(parts)).write_text('\\n'.join(str(i%parts) for i in range(count))+'\\n')
''')
    fake.chmod(0o755)
    initialized=tmp_path/'tritonpart-qualification'
    result=subprocess.run([sys.executable,str(script),'--ir',str(args[0].path),'--platform',str(platform),
        '--timing-database',str(timing),'--oracle',str(patron_ingress_oracle()),'--out',str(initialized),
        '--expected-design',args[0].value['design']['name'],'--initialization','tritonpart','--openroad',str(fake)],
        capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    summary=read_json(initialized/'qualification.json')
    assert summary['initial_ownership']=='normal-generalized-tritonpart-seed0-initializer'
    assert summary['arms']['direct']['signature']==summary['arms']['snapshot']['signature']
    assert not (initialized/'tmp').exists()
    failure=tmp_path/'wrong-design'
    rejected=subprocess.run([sys.executable,str(script),'--ir',str(args[0].path),'--platform',str(platform),
        '--timing-database',str(timing),'--oracle',str(patron_ingress_oracle()),'--out',str(failure),
        '--expected-design','another-design'],capture_output=True,text=True)
    assert rejected.returncode!=0
    assert not (failure/'tmp').exists()
    assert not (failure/'qualification.json').exists()
    assert read_json(failure/'failure.json')['status']=='fail'


def test_selected_primary_owner_does_not_fall_back_when_reference_is_lost(inputs,tmp_path):
    from emuflow.errors import ValidationError
    args,initial,timing=inputs
    clusters=build_clusters(args[0],args[3],cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    direct_args=(*args[:2],clusters,*args[3:])
    model=build_partition_pressure_model(*direct_args,independent_validation=False,
                                       timing_database_path=timing,native_store_model=True)
    model.primary_artifact=None
    with pytest.raises(ValidationError,match='selected direct primary ownership is unavailable'):
        run(direct_args,model,initial)
    clusters['clusters'].native_artifact=None
    with pytest.raises(ValidationError,match='native primary artifact reference is unavailable'):
        build_partition_pressure_model(*direct_args,independent_validation=False,
                                       timing_database_path=timing,native_store_model=True)


@pytest.mark.parametrize('max_moves',[0,2])
def test_native_model_matches_reference_without_python_graph_or_protocol(inputs,max_moves):
    args,initial,timing=inputs
    reference=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing)
    expected_counts={k:len(reference[v]) for k,v in [('clusters','clusters'),('nets','nets'),('paths','paths'),('capacity_domains','capacities')]}
    expected,reference_trace=run(args,reference,initial,max_moves)
    with (patch('emuflow.partition_pressure._compact_partition_pressure_graph',side_effect=AssertionError('Python graph')),
         patch('emuflow.partition_pressure._write_patron_native_input',side_effect=AssertionError('Python native model writer')),
         patch('emuflow.partition_pressure._native_dimensions',side_effect=AssertionError('Python cluster scan'))):
        model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
        assert isinstance(model,NativePressureModel)
        actual,trace=run(args,model,initial,max_moves)
    assert dict(actual['cluster_assignment'])==dict(expected['cluster_assignment'])
    assert trace['initial_metrics']==reference_trace['initial_metrics']
    assert trace['final_metrics']==reference_trace['final_metrics']
    assert trace['model_counts']==expected_counts
    assert trace['native_input_transport']=='native-store-model-v1'
    assert trace['change_count']==reference_trace['change_count']
    for value in trace['native_model_metrics'].values():
        assert value['wall_seconds']>0
        assert value['rss_scope']=='self-lifetime-high-water'


def test_every_native_model_record_matches_independent_python_model(inputs,tmp_path):
    args,initial,timing=inputs
    reference=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing)
    expected=tmp_path/'reference.bin'
    _write_patron_native_input(None,args[1],args[2],args[3],args[5],reference,initial,2,14,
                              _writer=_NativeBinaryPipeWriter(expected.open('wb')))
    model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    columns,board=model.write_requests(tmp_path,initial,2)
    result=subprocess.run([str(patron_ingress_oracle()),str(args[0].path),str(model.timing_path),str(columns),str(board),str(expected)],
                          capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout=='NATIVE_MODEL_RECORD_PARITY_PASS\n'


def test_native_model_summary_schema(inputs,tmp_path):
    jsonschema=pytest.importorskip('jsonschema')
    from referencing import Registry,Resource
    args,initial,timing=inputs
    model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    columns,board=model.write_requests(tmp_path,initial,0)
    output=tmp_path/'result'
    result=subprocess.run([str(emuflow_native()),'phase3','refine-patron-from-store',str(args[0].path),str(model.timing_path),str(columns),str(board),str(output)],
                          capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    schemas=Path(__file__).resolve().parents[1]/'schemas'
    operation=schemas/'native-stage-result-v1.schema.json'
    registry=Registry().with_resource(operation.as_uri(),Resource.from_contents(json.loads(operation.read_text())))
    path=schemas/'patron-native-model-v1.schema.json'
    jsonschema.Draft202012Validator(dict(json.loads(path.read_text()),**{'$id':path.as_uri()}),registry=registry).validate(
        json.loads(Path(str(output)+'.summary.json').read_text()))


def test_phase3_production_selects_native_model_and_keeps_independent_validation(inputs,tmp_path):
    from emuflow.phase3 import run_phase3
    args,initial,timing=inputs
    platform=tmp_path/'platform.json'; write_json(platform,args[1].to_dict())
    routes=tmp_path/'routes.json'; write_json(routes,args[5])
    solution=tmp_path/'initial.part.2'
    solution.write_text('\n'.join('0' if initial['cluster_assignment'][c['id']]=='a' else '1'
                                   for c in args[2]['clusters'])+'\n')
    with (patch('emuflow.partition_pressure._compact_partition_pressure_graph',side_effect=AssertionError('Python pressure graph')),
          patch('emuflow.partition_pressure._write_patron_native_input',side_effect=AssertionError('Python model protocol'))):
        report=run_phase3(args[0].path,platform,tmp_path/'phase3',provider='patron',cut_mode=CUT_MODE_STATIC_EXACT,
                         min_used_fpgas=2,balance_tolerance=1.0,patron_algorithm_version=14,patron_max_moves=0,
                         tritonpart_solution=solution,route_constraints_path=routes,timing_database_path=timing,
                         hop_refiner=str(hop_partition_refiner()))
    assert report['status']=='pass'
    assert report['cluster_implementation_id']=='native-primary-clusters-v1'
    assert report['native_cluster_metrics']['wall_seconds']>0
    assert report['candidate_implementation_id']=='native-candidate-ids-v1'
    assert report['native_candidate_metrics']['wall_seconds']>0
    assert report['algorithm_validation']['native_input_transport']=='native-primary-store-model-v2'
    assert report['algorithm_validation']['model']['nets']==2
    assert report['algorithm_validation']['native_model_metrics']['model_build']['wall_seconds']>0


@pytest.mark.parametrize('corruption',['checksum','owner','source','path-order'])
def test_invalid_sources_are_rejected_without_publishing(inputs,tmp_path,corruption):
    args,initial,timing=inputs
    model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    columns,board=model.write_requests(tmp_path,initial,0)
    if corruption in ('checksum','owner'):
        data=bytearray(columns.read_bytes())
        if corruption=='checksum': data[-1]^=1
        else:
            # Header28 bytes; member_offsets has C+1 uint32 entries.
            offset=28+4*(len(model.clusters)+1)
            struct.pack_into('<I',data,offset,len(model.clusters))
            struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
        columns.write_bytes(data)
        # Rebind only the columns generation in the private board request so
        # the malformed payload, rather than stale-reference check, is tested.
        from emuflow.native_tools import native_file_generation
        request=bytearray(board.read_bytes())
        struct.pack_into('<3Q4q',request,12+2*56,*native_file_generation(columns))
        struct.pack_into('<I',request,len(request)-4,zlib.crc32(request[:-4]))
        board.write_bytes(request)
    elif corruption=='source':
        with closing(sqlite3.connect(args[0].path)) as db,db:
            db.execute("UPDATE net_records SET cut_class='invalid' WHERE seq=0")
    else:
        with closing(sqlite3.connect(model.timing_path)) as db,db:
            db.execute('UPDATE path_nets SET net_ordinal=7 WHERE path_ordinal=0')
        from emuflow.native_tools import native_file_generation
        request=bytearray(board.read_bytes())
        struct.pack_into('<3Q4q',request,12+56,*native_file_generation(model.timing_path))
        struct.pack_into('<I',request,len(request)-4,zlib.crc32(request[:-4]))
        board.write_bytes(request)
    output=tmp_path/'result'; output.write_text('previous')
    command=[str(emuflow_native()),'phase3','refine-patron-from-store',str(args[0].path),str(model.timing_path),str(columns),str(board),str(output)]
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_text()=='previous'
    assert not Path(str(output)+'.order').exists()
    assert not Path(str(output)+'.summary.json').exists()
    assert not list(tmp_path.glob('*.stage.*'))
