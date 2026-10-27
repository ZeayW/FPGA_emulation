"""Source launch context shares Phase2 bytes without extracting paths again."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.indexed_timing import IndexedTimingView
from emuflow.opensta import _run_opensta_path_database
from emuflow.opensta import DEFAULT_TIMING_MODEL
from emuflow.opensta_source_context import emit_opensta_source_context
from emuflow.source_launch_clocks import build_source_launch_clock_store
from emuflow.store_publication import bind_store_source
from tests.native_build import emuflow_native
from tests.test_native_source_launch_clocks import prepared

IO = {'input_groups':[{'clock':'clk','delay_ns':0.,'ports':['rst']}], 'output_groups':[]}


@pytest.fixture
def context(prepared,monkeypatch,tmp_path):
    source,config,_=prepared
    identity=dict(name='OpenSTA',version='fixture',executable_sha256=hashlib.sha256(Path(config['engine']).read_bytes()).hexdigest())
    monkeypatch.setattr('emuflow.opensta_source_context.require_opensta_engine',lambda _:identity)
    monkeypatch.setattr('emuflow.opensta.require_opensta_engine',lambda _:identity)
    model=tmp_path/'model.json';shutil.copyfile(DEFAULT_TIMING_MODEL,model)
    return emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),tmp_path/'context',
        clocks={'clk':10.},timing_io=IO,executable=config['engine'],timing_model_path=model)


def test_source_context_and_phase2_emit_identical_model_clock_and_io_bytes(context,prepared,monkeypatch,tmp_path):
    source,config,_=prepared;observed={}
    class Captured(Exception):pass
    def capture(command,*,environment):
        for key in ['LIBERTY','VERILOG','CLOCKS','TIMING_IO']:
            observed[key]=Path(environment['EMUFLOW_STA_'+key]).read_bytes()
        raise Captured()
    monkeypatch.setattr('emuflow.opensta.run_with_bounded_output',capture)
    view=IndexedTimingView(source)
    try:
        with pytest.raises(Captured):
            _run_opensta_path_database(source,tmp_path/'paths.json',clocks={'clk':10.},timing_io=IO,
                executable=config['engine'],clock_budget_policy='engine-edge-v1',_stream_output=True,_ir=view)
    finally:view.close()
    assert observed=={k:Path(context.environment['EMUFLOW_STA_'+k]).read_bytes()for k in observed}
    assert context.reference_clocks_ns=={'clk':10.}
    assert context.model['clock_logic']['policy']=='indexed-init-clock-sense-v1'
    assert 'FDRE__NEG' in Path(context.environment['EMUFLOW_STA_VERILOG']).read_text()
    assert context.timing_io['input_ports']==1


def test_context_has_exact_sealed_dependencies_and_no_second_graph_or_path_enumeration(prepared,context,monkeypatch,tmp_path):
    monkeypatch.setattr('emuflow.ir.EmuIR.load',lambda *_:pytest.fail('whole graph loaded'))
    monkeypatch.setattr('emuflow.opensta._run_opensta_path_database',lambda *_a,**_k:pytest.fail('paths extracted again'))
    source,config,_=prepared
    value=emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),tmp_path/'another',
        clocks={'clk':10.},timing_io=IO,executable=config['engine'])
    assert len(value.timing_inputs)==9
    assert {p for p in value.timing_inputs}=={b.path for b in value.source_bindings if b.path!=source.resolve()}
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==seal for p,seal in value.timing_inputs.items())
    value.require_unchanged()


def test_capture_context_reuses_source_bytes_and_seals_its_own_export_entry(context,prepared,monkeypatch,tmp_path):
    source,config,_=prepared
    monkeypatch.setattr('emuflow.ir.EmuIR.load',lambda *_:pytest.fail('whole graph loaded'))
    monkeypatch.setattr('emuflow.opensta._run_opensta_path_database',lambda *_a,**_k:pytest.fail('paths extracted again'))
    monkeypatch.setattr('emuflow.opensta_source_context._prepare_opensta_source_model',
        lambda *_a,**_k:pytest.fail('source graph emitted again'))
    value=context.for_clock_query('state-capture')
    assert Path(value.command[-1]).name=='export_capture_clock_edges.tcl'
    for key in ('LIBERTY','VERILOG','CLOCKS','TIMING_IO'):
        assert value.environment['EMUFLOW_STA_'+key]==context.environment['EMUFLOW_STA_'+key]
    names={p.name for p in value.timing_inputs}
    assert {'export_capture_clock_edges.tcl','capture_clock_edges.tcl','design_context.tcl'}<=names
    assert not {'export_launch_clock_edges.tcl','launch_clock_edges.tcl'}&names
    assert value.reference_clocks_ns==context.reference_clocks_ns
    assert value.timing_io==context.timing_io
    value.require_unchanged()
    restored=value.for_clock_query('launch')
    assert restored.command==context.command and restored.timing_inputs==context.timing_inputs
    assert {binding.path:binding for binding in restored.source_bindings}=={binding.path:binding for binding in context.source_bindings}


def test_unknown_clock_query_kind_is_rejected_before_creating_a_context(prepared,context,tmp_path):
    source,config,_=prepared;output=tmp_path/'unknown-query'
    with pytest.raises(ValidationError,match='query kind'):
        emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),output,
            clocks={'clk':10.},timing_io=IO,executable=config['engine'],query_kind='guess')
    assert not output.exists()
    with pytest.raises(ValidationError,match='query kind'):
        context.for_clock_query('guess')


def test_derived_capture_context_retains_the_new_script_generation_lease(context,tmp_path,monkeypatch):
    from emuflow import opensta_source_context as module
    original=module._clock_query_scripts
    scripts=original(Path(context.command[-1]).parent,'state-capture')
    copied=[]
    for path in scripts:
        target=tmp_path/path.name;shutil.copyfile(path,target);copied.append(target)
    monkeypatch.setattr(module,'_clock_query_scripts',lambda root,kind:
        tuple(copied) if kind=='state-capture' else original(root,kind))
    derived=context.for_clock_query('state-capture');derived.require_unchanged()
    os.utime(copied[-1],None)
    with pytest.raises(ValidationError,match='generation changed'):derived.require_unchanged()


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='real OpenSTA not configured')
@pytest.mark.parametrize('damage',[None,'sequence','hex','absent','noncapture','top-port','header'])
@pytest.mark.parametrize('derive',[False,True])
def test_real_capture_export_uses_source_context_and_has_no_success_after_incomplete_requests(prepared,tmp_path,damage,derive):
    source,_,_=prepared
    value=emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),tmp_path/'real-captures',
        clocks={'clk':10.},timing_io=IO,executable=os.environ['EMUFLOW_TEST_OPENSTA'],
        query_kind='launch' if derive else 'state-capture')
    if derive:value=value.for_clock_query('state-capture')
    root=tmp_path/'export';root.mkdir()
    # These original pins are requested directly, without any timing-path
    # selector. The escaped Unicode name exercises the production transport.
    names=['positive/D','negative\\\\雪/hier/D']
    rows=[[str(i),pin.encode().hex()]for i,pin in enumerate(names)]
    header='capture_seq\tpin_hex'
    if damage=='sequence':rows[1][0]='2'
    if damage=='hex':rows[1][1]='zz'
    if damage=='absent':rows[1][1]='absent/D'.encode().hex()
    if damage=='noncapture':rows[1][1]='positive/Q'.encode().hex()
    if damage=='top-port':rows[1][1]='rst'.encode().hex()
    if damage=='header':header='source_seq\tpin_hex'
    requests=root/'requests.tsv';requests.write_text(header+'\n'+''.join('\t'.join(row)+'\n'for row in rows))
    output=root/'facts.tsv';catalog=root/'clocks.tsv'
    environment={**os.environ,**value.environment,'EMUFLOW_STA_TOP':'counter',
        'EMUFLOW_CAPTURE_REQUESTS':str(requests),'EMUFLOW_CAPTURE_OUTPUT':str(output),
        'EMUFLOW_CAPTURE_CLOCK_CATALOG':str(catalog)}
    value.require_unchanged()
    result=subprocess.run(value.command,env=environment,cwd=root,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    value.require_unchanged()
    marker='EMUFLOW_CAPTURE_CLOCK_EXPORT status=pass'
    if damage:
        assert result.returncode!=0 and marker not in result.stdout,result.stdout
    else:
        assert result.returncode==0 and result.stdout.count(marker)==1,result.stdout
        assert marker+' captures=2 facts=2 clocks=1' in result.stdout
        lines=[line.split('\t')for line in output.read_text().splitlines()]
        assert lines[0]==['capture_seq','clock_hex','period_ns','phase_ns']
        assert [(row[0],bytes.fromhex(row[1]).decode())for row in lines[1:]]==[('0','clk'),('1','clk')]
        assert [float(row[3])/float(row[2])for row in lines[1:]]==pytest.approx([0.,.5])
        assert all(float(row[2])==pytest.approx(10.)for row in lines[1:])
        catalog_rows=[line.split('\t')for line in catalog.read_text().splitlines()]
        assert catalog_rows[0]==['clock_seq','clock_hex','period_ns']
        assert len(catalog_rows)==2 and catalog_rows[1][:2]==['0','clk'.encode().hex()]
        assert float(catalog_rows[1][2])==pytest.approx(10.)


@pytest.mark.parametrize('change',['source','mapped','clock','model'])
def test_changed_generation_never_renews_a_context(context,prepared,change):
    source,_,_=prepared
    path=(source if change=='source' else Path(context.environment['EMUFLOW_STA_VERILOG'])if change=='mapped'
          else Path(context.environment['EMUFLOW_STA_CLOCKS'])if change=='clock'
          else next(p for p in context.timing_inputs if p.suffix=='.json'))
    os.utime(path,None)
    with pytest.raises(ValidationError,match='generation changed'):context.require_unchanged()


def test_stale_original_binding_is_rejected_before_emission(context,prepared,tmp_path):
    source,config,_=prepared;binding=bind_store_source(source,'emuir-compact-v2');os.utime(source,None)
    output=tmp_path/'stale'
    with pytest.raises(ValidationError,match='generation changed'):
        emit_opensta_source_context(binding,output,clocks={'clk':10.},timing_io=IO,executable=config['engine'])
    assert not output.exists()


def test_failed_emission_cleans_only_its_new_scratch(context,prepared,tmp_path):
    source,config,_=prepared;output=tmp_path/'invalid'
    with pytest.raises(ValidationError,match='absent'):
        emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),output,
            clocks={'missing':10.},timing_io=IO,executable=config['engine'])
    assert not output.exists() and source.is_file()
    output.mkdir();(output/'user.txt').write_text('keep')
    with pytest.raises(FileExistsError):
        emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),output,
            clocks={'clk':10.},timing_io=IO,executable=config['engine'])
    assert (output/'user.txt').read_text()=='keep'


def launch(prepared,context,output):
    source,config,_=prepared
    context.require_unchanged()
    return build_source_launch_clock_store(source,output,logic_costs=config['logic'],local_costs=config['local'],
        source_identity=dict(original_ir_sha256=config['original_ir_sha256'],path_database_sha256='b'*64,
            routes_sha256='c'*64,schedule_sha256='d'*64,semantic_sha256='e'*64,timing_io_sha256=context.timing_io['sha256']),
        command=context.command,environment={**os.environ,**context.environment},timing_inputs=context.timing_inputs,
        source_bindings=(*context.source_bindings,*(bind_store_source(Path(p),'logic-segment-timing-store-v3')for p in config['logic']),
                         *(bind_store_source(Path(p),'local-path-timing-store-v3')for p in config['local'])),
        executable=str(emuflow_native()))


def test_emitted_context_feeds_the_existing_native_clock_owner(context,prepared,tmp_path):
    output=launch(prepared,context,tmp_path/'emitted-clock-owner.sqlite')
    with closing(sqlite3.connect(output))as db:
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0]==3
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0]==4
        assert len(json.loads(db.execute("SELECT value FROM metadata WHERE key='timing_inputs'").fetchone()[0]))>=8


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),reason='explicit qualified OpenSTA launch API required')
def test_real_emitted_context_keeps_original_positive_and_negative_edges(prepared,tmp_path):
    source,_,_=prepared
    value=emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),tmp_path/'real-context',
        clocks={'clk':10.},timing_io=IO,executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'])
    output=launch(prepared,value,tmp_path/'real-owner.sqlite')
    with closing(sqlite3.connect(output))as db:
        rows=list(db.execute('SELECT s.cell_seq,f.phase_ns,c.engine_period_ns FROM sources s JOIN facts f USING(source_seq) JOIN clocks c USING(clock_seq) ORDER BY s.source_seq,f.phase_ns'))
        assert len(rows)==3
        phases=sorted(phase/period for _,phase,period in rows)
        assert phases==pytest.approx([0.,0.,.5])


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),reason='explicit qualified OpenSTA required')
def test_real_default_path_export_retains_endpoints_beyond_a_diagnostic_cap(prepared,tmp_path):
    from emuflow.opensta import run_opensta_path_database
    source,_,_=prepared
    common=dict(clocks={'clk':10.},timing_io={**IO,
        'output_groups':[{'clock':'clk','delay_ns':0.,'ports':['q']}]},
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
        _stream_output=True,clock_budget_policy='engine-edge-v1')
    capped=run_opensta_path_database(source,tmp_path/'capped.json',max_paths=1,**common)
    complete=run_opensta_path_database(source,tmp_path/'all-endpoints.json',**common)
    assert capped['paths']==1 and capped['path_limit_reached']
    assert complete['paths']>capped['paths'] and not complete['path_limit_reached']
    assert complete['path_selection']['max_paths_per_group'] is None
