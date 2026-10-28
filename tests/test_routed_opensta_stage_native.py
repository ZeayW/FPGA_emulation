"""Complete routed OpenSTA control and independent typed-path acceptance."""
import json
import math
from pathlib import Path
from unittest import mock

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.timing.xilinx_opensta import run_xilinx_routed_opensta, validate_xilinx_routed_opensta_summary
from tests.test_xilinx_opensta import _indexed_routed_fixture
from tests.test_phase2_native import _fixed_opensta


def records(slacks=(-0.25,-0.125,0.1)):
    return [[f'lut/O->ff/D#{i:08d}'.encode().hex(),'clk'.encode().hex(),'10',repr(slack),repr(10-slack),
        ','.join(n.encode().hex() for n in ('n','__emuflow_rw_delay_net__00000000'))] for i,slack in enumerate(slacks)]


def run(root,**kwargs):
    mapped,timing=_indexed_routed_fixture(root)
    engine,trace=_fixed_opensta(root,records(),**kwargs)
    options=dict(mapped_path=mapped,timing_path=timing,output_path=root/'paths.json',summary_path=root/'summary.json',
        clocks={'clk':10.0},executable=str(engine),log_path=root/'opensta.log')
    return options,trace


def test_complete_stage_uses_native_graph_model_import_and_validation(tmp_path):
    options,trace=run(tmp_path)
    from emuflow.timing import opensta
    with (mock.patch.object(opensta,'run_opensta_path_database',side_effect=AssertionError('Python OpenSTA coordinator')),
          mock.patch.object(opensta,'render_opensta_liberty',side_effect=AssertionError('Python Liberty writer'))):
        check=run_xilinx_routed_opensta(**options)
    assert check['native_owner']['implementation_id']=='native-routed-opensta-stage-v1'
    assert check['wns_ns']==-0.25 and check['tns_ns']==-0.375
    assert check['critical_path_ns']==10.25
    assert check['failing_endpoints']==2 and check['timed_endpoints']==3
    summary=json.loads(options['summary_path'].read_text())
    assert summary['authority']=='opensta' and not summary['opensta']['path_limit_reached']
    assert summary['source']['mapped_sha256']==file_sha256(options['mapped_path'])
    assert summary['source']['routed_timing_sha256']==file_sha256(options['timing_path'])
    assert summary['source']['timing_path_database_sha256']==file_sha256(options['output_path'])
    assert summary['staging']['implementation_id']=='native-routed-opensta-v1'
    assert summary['opensta']['path_import']['implementation_id']=='native-sta-path-import-v1'
    second=validate_xilinx_routed_opensta_summary(options['summary_path'],output_path=options['output_path'],
        mapped_path=options['mapped_path'],timing_path=options['timing_path'])
    for key in ('wns_ns','tns_ns','failing_endpoints','timed_endpoints','critical_path_ns','summary_sha256'):
        assert second[key]==check[key]
    events=[json.loads(r) for r in trace.read_text().splitlines()]
    assert [e['kind'] for e in events]==['version','runtime','paths']
    assert not list(tmp_path.glob('.routed-opensta.generation.*'))


def test_native_liberty_matches_the_independent_current_model(tmp_path):
    options,trace=run(tmp_path);run_xilinx_routed_opensta(**options)
    from tests.routed_opensta_reference import build_xilinx_routed_opensta_indexed_inputs
    from emuflow.timing.opensta import render_opensta_liberty
    model,_=build_xilinx_routed_opensta_indexed_inputs(options['mapped_path'],options['timing_path'],tmp_path/'reference.sqlite',clocks=options['clocks'])
    event=[json.loads(r) for r in trace.read_text().splitlines() if json.loads(r)['kind']=='paths'][0]
    assert event['inputs']['EMUFLOW_STA_LIBERTY']==render_opensta_liberty(model.base_model)


@pytest.mark.parametrize('failure',['exit','missing-tsv'])
def test_failed_execution_keeps_prior_accepted_artifacts(tmp_path,failure):
    options,trace=run(tmp_path);run_xilinx_routed_opensta(**options)
    paths=[options[k] for k in ('output_path','summary_path','log_path')]
    previous={p:p.read_bytes() for p in paths}
    _fixed_opensta(tmp_path,records(),failure=failure)
    with pytest.raises(ValidationError):run_xilinx_routed_opensta(**options)
    for p,data in previous.items():assert p.read_bytes()==data
    assert list(tmp_path.glob('.routed-opensta.generation.*/*.log'))


def test_exact_tns_and_native_path_limit_rejection(tmp_path):
    options,_=run(tmp_path)
    slacks=(-1e16,-1.,-1.,-0.1,-0.2,0.5)
    _fixed_opensta(tmp_path,records(slacks))
    check=run_xilinx_routed_opensta(**options)
    assert check['tns_ns']==math.fsum(s for s in slacks if s<0)
    previous=options['summary_path'].read_bytes()
    with pytest.raises(ValidationError,match='path limit was reached'):run_xilinx_routed_opensta(**options,max_paths=len(slacks))
    assert options['summary_path'].read_bytes()==previous


@pytest.mark.parametrize('inverted',[False,True])
def test_native_model_preserves_hardblock_and_clock_policies(tmp_path,inverted):
    import sqlite3
    options,trace=run(tmp_path)
    with sqlite3.connect(options['mapped_path']) as db:
        if inverted:
            db.execute("UPDATE cells SET record_json=json_set(record_json,'$.parameters.IS_C_INVERTED','1') WHERE id='ff'")
        clock=db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='ff' AND port='C'").fetchone()[0]
        signal=db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='lut' AND port='I0'").fetchone()[0]
        seq=db.execute('SELECT max(seq)+1 FROM cells').fetchone()[0]
        net_seq=db.execute('SELECT max(seq)+1 FROM netnames').fetchone()[0]
        for kind,ports in [
            ('DSP48E2',{'A':[signal,signal],'P':[net_seq+2,net_seq+3]}),
            ('RAMB18E2',{'CLKARDCLK':[clock],'ADDRARDADDR':[signal],'DOADO':[net_seq+4]}),
            ('RAMB36E2',{'CLKARDCLK':[clock],'ADDRARDADDR':[signal],'DOADO':[net_seq+5]}),
            ('URAM288',{'CLK':[clock],'SLEEP':['0'],'ADDR_A':[signal],'DOUT_A':[net_seq+6]})]:
            name='hard_'+kind
            record={'type':kind,'parameters':{},'attributes':{},'hide_name':0}
            db.execute('INSERT INTO cells VALUES (?,?,?,?)',(name,seq,kind,json.dumps(record)))
            for port,bits in ports.items():
                direction=1 if port in {'P','DOADO','DOUT_A'} else 0
                for ordinal,bit in enumerate(bits):
                    integer=isinstance(bit,int)
                    db.execute('INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)',
                        (seq,name,port,ordinal,len(bits),direction,0 if integer else 1,bit if integer else None,None if integer else bit))
            seq+=1
        for i in range(5):db.execute('INSERT INTO netnames VALUES (?,?,?)',('hard_net_'+str(i),net_seq+i,json.dumps({'bits':[net_seq+i+2]})))
    timing=json.loads(options['timing_path'].read_text());timing['source']['mapped_sha256']=file_sha256(options['mapped_path'])
    options['timing_path'].write_text(json.dumps(timing))
    run_xilinx_routed_opensta(**options)
    from tests.routed_opensta_reference import build_xilinx_routed_opensta_indexed_inputs
    from emuflow.timing.opensta import render_opensta_liberty
    model,_=build_xilinx_routed_opensta_indexed_inputs(options['mapped_path'],options['timing_path'],tmp_path/'reference.sqlite',clocks=options['clocks'])
    event=[json.loads(r) for r in trace.read_text().splitlines() if json.loads(r)['kind']=='paths'][0]
    assert event['inputs']['EMUFLOW_STA_LIBERTY']==render_opensta_liberty(model.base_model)
    summary=json.loads(options['summary_path'].read_text())
    assert summary['staging']['hard_block_types_unqualified']==['DSP48E2','RAMB18E2','RAMB36E2','URAM288']
    assert summary['staging']['falling_edge_ff_instances']==int(inverted)
    assert summary['qualification']['hard_block_timing']=='zero-delay-sequential-surrogate-unqualified'
    assert summary['qualification']['hold_analysis']=='unavailable'


@pytest.mark.parametrize('damage',['wns','tns','failing','timed','path-seal','mapped-seal','timing-seal','payload-seal','payload-records'])
def test_independent_validator_rejects_tampered_current_evidence(tmp_path,damage):
    options,_=run(tmp_path);run_xilinx_routed_opensta(**options)
    summary=json.loads(options['summary_path'].read_text())
    if damage in {'wns','tns','failing','timed'}:
        key={'wns':'wns_ns','tns':'tns_ns','failing':'failing_endpoints','timed':'timed_endpoints'}[damage]
        summary['qor'][key]+=1
    elif damage in {'path-seal','mapped-seal','timing-seal'}:
        key={'path-seal':'timing_path_database_sha256','mapped-seal':'mapped_sha256','timing-seal':'routed_timing_sha256'}[damage]
        summary['source'][key]='f'*64
    else:
        selector=json.loads(options['output_path'].read_text());descriptor=selector['payloads']['paths']
        if damage=='payload-seal':descriptor['sha256']='0'*64
        else:descriptor['records']+=1
        options['output_path'].write_text(json.dumps(selector))
        summary['source']['timing_path_database_sha256']=file_sha256(options['output_path'])
    options['summary_path'].write_text(json.dumps(summary))
    with pytest.raises(ValidationError):
        validate_xilinx_routed_opensta_summary(options['summary_path'],output_path=options['output_path'],
            mapped_path=options['mapped_path'],timing_path=options['timing_path'])


@pytest.mark.parametrize('source',['mapped','timing','payload','derived-liberty'])
def test_sources_cannot_change_inside_the_tool_transaction(tmp_path,source):
    options,_=run(tmp_path);run_xilinx_routed_opensta(**options)
    old={options[k]:options[k].read_bytes() for k in ('output_path','summary_path','log_path')}
    if source=='payload':
        doc=json.loads(options['timing_path'].read_text());path=options['timing_path'].parent/doc['payloads']['endpoints']['path']
    elif source=='derived-liberty':path=None
    else:path=options[source+'_path']
    engine,_=_fixed_opensta(tmp_path,records(),replace_source=path)
    if source=='derived-liberty':
        script=engine.read_text().replace("print('FIXED_OPENSTA extracted records=3')",
            "Path(os.environ['EMUFLOW_STA_LIBERTY']).write_text('changed by tool')\nprint('changed derived input')")
        engine.write_text(script)
    with pytest.raises(ValidationError,match='generation changed|input changed|source changed'):
        run_xilinx_routed_opensta(**options)
    for path,data in old.items():assert path.read_bytes()==data


@pytest.mark.parametrize('owner',['selector','payload','summary'])
def test_host_refuses_replaced_output_generations_after_native_acceptance(tmp_path,owner):
    options,_=run(tmp_path)
    from emuflow.common.tool_runtime import NativeToolProcess
    finish=NativeToolProcess.finish
    def changed(process):
        result=finish(process)
        path=options['output_path'] if owner in {'selector','payload'} else options['summary_path']
        if owner=='payload':
            doc=json.loads(path.read_text());path=path.parent/doc['payloads']['paths']['path']
        path.chmod(path.stat().st_mode^0o100)
        return result
    with mock.patch.object(NativeToolProcess,'finish',changed),pytest.raises(ValidationError,match='output generation changed'):
        run_xilinx_routed_opensta(**options)


@pytest.mark.parametrize('field',['mapped_sha256','routed_timing_sha256'])
def test_wrong_cached_source_hash_is_rejected(tmp_path,field):
    options,trace=run(tmp_path)
    with pytest.raises(ValidationError,match='cached source identity disagrees'):
        run_xilinx_routed_opensta(**options,source_sha256={field:'0'*64})
    assert [json.loads(r)['kind'] for r in trace.read_text().splitlines()]==['version','runtime']
    assert not options['output_path'].exists()


def test_whole_stage_keeps_the_host_bounded_with_twenty_thousand_delay_types(tmp_path):
    import sqlite3
    import tracemalloc
    options,_=run(tmp_path);count=20000
    with sqlite3.connect(options['mapped_path']) as db:
        seq=db.execute('SELECT max(seq)+1 FROM cells').fetchone()[0]
        for ordinal in range(1,count):
            name=f'clone_{ordinal:06d}'
            db.execute("INSERT INTO cells SELECT ?,?,type,record_json FROM cells WHERE id='ff'",(name,seq))
            db.execute("INSERT INTO cell_pins SELECT ?,?,port,port_bit,port_width,direction,value_kind,net_bit,constant FROM cell_pins WHERE cell_id='ff' AND port!='Q'",(seq,name))
            seq+=1
    doc=json.loads(options['timing_path'].read_text());doc['source']['mapped_sha256']=file_sha256(options['mapped_path'])
    payload=options['timing_path'].parent/doc['payloads']['endpoints']['path']
    with payload.open('w') as stream:
        for ordinal in range(count):
            name='ff' if ordinal==0 else f'clone_{ordinal:06d}'
            stream.write(json.dumps({'id':f'n:{name}/D','net':'n','mapped_bit':4,
                'driver':{'instance':'lut','pin':'O','site':'S0'},'sink':{'instance':name,'pin':'D','site':'S1'},
                'route_delay_ns':ordinal/100000,'binding':'exact-site-pin'})+'\n')
    doc['payloads']['endpoints'].update(records=count,sha256=file_sha256(payload))
    doc['summary'].update(logical_endpoints=count,maximum_route_delay_ns=(count-1)/100000)
    options['timing_path'].write_text(json.dumps(doc));del doc
    tracemalloc.start()
    try:
        check=run_xilinx_routed_opensta(**options)
        _current,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert peak<16*1024*1024
    summary=json.loads(options['summary_path'].read_text())
    assert summary['staging']['inserted_route_delay_cells']==count
    assert summary['staging']['unique_route_delay_cells']==count
    assert check['timed_endpoints']==3


def test_validation_requires_only_read_access_to_the_published_directory(tmp_path):
    options,_=run(tmp_path);run_xilinx_routed_opensta(**options)
    mode=tmp_path.stat().st_mode
    tmp_path.chmod(0o500)
    try:
        check=validate_xilinx_routed_opensta_summary(options['summary_path'],output_path=options['output_path'])
        assert check['tns_ns']==-0.375
    finally:tmp_path.chmod(mode)


def test_large_opaque_manifest_metadata_is_not_a_python_control_document(tmp_path):
    # Staging does not consume clock-gate metadata; this fixture checks artifact
    # size/host ownership and does not establish physical qualification.
    options,_=run(tmp_path)
    doc=json.loads(options['timing_path'].read_text())
    doc['clock_gates']=[{'logical_cell':f'opaque_{i:06d}','metadata':'x'*500} for i in range(20000)]
    options['timing_path'].write_text(json.dumps(doc));del doc
    assert options['timing_path'].stat().st_size>8*1024*1024
    check=run_xilinx_routed_opensta(**options)
    assert check['timed_endpoints']==3 and check['tns_ns']==-0.375


def test_an_ambient_container_binding_cannot_reidentify_a_direct_engine(tmp_path,monkeypatch):
    options,_=run(tmp_path)
    monkeypatch.setenv('EMUFLOW_OPENSTA_CONTAINER_BINARY',str(tmp_path/'absent-other-engine'))
    run_xilinx_routed_opensta(**options)
    identity=json.loads(options['summary_path'].read_text())['opensta']['engine']
    assert identity['executable_sha256']==file_sha256(Path(options['executable']))
    assert 'launcher_sha256' not in identity


def test_container_selection_uses_exact_launcher_bytes_and_an_explicit_binary(tmp_path,monkeypatch):
    from emuflow.timing import xilinx_opensta as native
    launcher=native._runtime_data_path(Path('scripts/opensta/container_runtime.sh'))
    selected=tmp_path/'renamed-launcher';selected.write_bytes(launcher.read_bytes());selected.chmod(0o755)
    binary=tmp_path/'versioned-sta';binary.write_text('# versioned engine\n')
    options=dict(mapped_path=tmp_path/'mapped',timing_path=tmp_path/'timing',output_path=tmp_path/'paths',
                 summary_path=tmp_path/'summary',clocks={'clk':10.0},executable=str(selected))
    monkeypatch.delenv('EMUFLOW_OPENSTA_CONTAINER_BINARY',raising=False)
    with pytest.raises(ValidationError,match='lacks an explicit binary'):
        run_xilinx_routed_opensta(**options)
    monkeypatch.setenv('EMUFLOW_OPENSTA_CONTAINER_BINARY',str(binary))
    with mock.patch.object(native,'_invoke',return_value={'status':'pass'}) as invoke:
        run_xilinx_routed_opensta(**options)
    rows={r[0]:r[1:] for r in invoke.call_args.args[1]}
    assert Path(rows['container'][0])==binary
    assert rows['container'][1]=='external-tool-executable-v1'
