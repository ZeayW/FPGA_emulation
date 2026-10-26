"""Source launch context shares Phase2 bytes without extracting paths again."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3

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
    common=dict(clocks={'clk':10.},timing_io=IO,
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
        _stream_output=True,clock_budget_policy='engine-edge-v1')
    capped=run_opensta_path_database(source,tmp_path/'capped.json',max_paths=1,**common)
    complete=run_opensta_path_database(source,tmp_path/'all-endpoints.json',**common)
    assert capped['paths']==1 and capped['path_limit_reached']
    assert complete['paths']>capped['paths'] and not complete['path_limit_reached']
    assert complete['path_selection']['max_paths_per_group'] is None
