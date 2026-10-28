"""Phase7c consumes native observations and keeps timing metric scopes clear."""
import csv
import json
import os
import sqlite3
from pathlib import Path
import pytest
from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.native_word_phase7c import _clock_context,run_native_word_phase7c
from tests.test_native_word_source_catalog import source,prepare
from tests.test_native_word_costs import HEADER
from tests.test_native_word_tx_binding import row


def reference(path):return {'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}


def physical(source,*,transit=False):
    evidence={}
    for fpga in ('fpga0','fpga1'):
        # These transport-role fixtures normally mock the mapped reader. The
        # end-to-end case also supplies its real registered storage envelope.
        with sqlite3.connect(source.mapped[fpga])as db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES(?,?)',('schema','emuflow.xilinx-mapped-index/v2'))
        preparation=prepare(source,fpga);roles=Path(preparation['directory'])
        output=source.root/(fpga+'-observation');output.mkdir()
        clock='emuflow_dut_clk_0';clockless=transit and fpga=='fpga1'
        clock_contract=output/'clock-contract.tsv'
        clock_contract.write_text('clock_hex\tport_hex\tperiod_ns\n'+
            b'emuflow_tdm_clk'.hex()+'\t'+b'fabric_clk'.hex()+'\t5\n'+
            (''if clockless else clock.encode().hex()+'\t'+b'clk'.hex()+'\t100\n'))
        files={};tx=[];fanins=[]
        with(roles/'targets.tsv').open(newline='')as stream:
            for target in csv.DictReader(stream,delimiter='\t'):
                phase=int(target['phase_word']);query=bytes.fromhex(target['query_hex']).decode()
                if clockless:tx.append(row(phase,query,kind='rx',source='s9',edge='',cost=2.))
                else:tx.extend([row(phase,query,source=clock,cost=1.),row(phase,query,source=clock,edge='fall',measured=False)])
                fanins.append(f"{phase}\t{target['query_hex']}\t1\t{int(clockless)}\t{int(not clockless)}\t0\t0\t0\n")
        captures=[]
        if not clockless:
            for edge in ('rise','fall'):
                query=f'dut-capture:{clock}:{edge}'
                captures.extend(row(-1,query,source=clock,edge=launch,measured=False)for launch in('rise','fall'))
                capture=row(-1,query,kind='rx',source='s9',edge='',cost=2.,measured=edge=='rise')
                if edge=='rise':capture[13]=clock.encode().hex()
                captures.append(capture)
        for name,rows in [('native-costs.tsv',tx),('native-capture-costs.tsv',captures)]:
            path=output/name;path.write_text(HEADER+'\n'+''.join('\t'.join(r)+'\n'for r in rows));files[name]=reference(path)
        path=output/'fanin-summary.tsv';path.write_text('phase_word\tquery_hex\tnative_startpoints\trx_sources\tdut_cells\tinfrastructure_cells\tports\tunclassified\n'+''.join(fanins));files[path.name]=reference(path)
        path=output/'missing-rx-roles.tsv';path.write_text('source_origin\n');files[path.name]=reference(path)
        observation={'status':'complete','fpga':fpga,'source_directory':str(roles),
            'counts':{'dut_clocks':0 if clockless else 1},'files':files,'source_generations':[]}
        evidence[fpga]={'sources':preparation,'observation':observation,'input_identity':{
            'word_schedule':reference(source.schedule_path),'mapped_index':reference(source.mapped[fpga])},
            'implementation_timing':{'tdm_clock':{'period_ns':5.},'dut_clock_periods_ns':{'clk':100.}},
            'clock_contract':reference(clock_contract)}
    return {'native_word_observations':evidence}


@pytest.mark.parametrize('transit',[False,True])
def test_clock_context_uses_shared_target_and_actual_local_families(source,transit):
    summary=physical(source,transit=transit)
    clocks,windows,period,targets=_clock_context(summary['native_word_observations'],[])
    assert period==100000 and targets=={'clk':100.}
    assert clocks['fpga0']=={'emuflow_dut_clk_0':'clk'}
    assert clocks['fpga1']==({}if transit else clocks['fpga0'])
    assert windows==[('clk',a,'clk',b,100000 if a==b else 50000)for a in(1,2)for b in(1,2)]


@pytest.mark.parametrize('damage',['period','family','artifact'])
def test_clock_context_does_not_guess_generated_or_different_clock_relationships(source,damage):
    summary=physical(source);evidence=summary['native_word_observations']
    if damage=='period':evidence['fpga1']['implementation_timing']['dut_clock_periods_ns']['clk']=200.
    if damage=='family':evidence['fpga1']['observation']['counts']['dut_clocks']=2
    if damage=='artifact':Path(evidence['fpga1']['clock_contract']['path']).write_text('other clock owner\n')
    with pytest.raises(ValidationError):_clock_context(evidence,[])


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA')or not os.environ.get('EMUFLOW_TEST_NATIVE_WORD_DRIVER'),
                    reason='qualified native runtime and independent OpenSTA required')
@pytest.mark.parametrize('transit',[False,True])
def test_native_phase7c_projects_real_graph_and_replays_independent_witnesses(source,monkeypatch,transit):
    native=Path(os.environ['EMUFLOW_TEST_NATIVE_WORD_DRIVER']);monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native.parent.parent))
    summary=physical(source,transit=transit)
    from emuflow.store_publication import bind_store_source
    # A multi-FPGA observer can have more ancestors than a single index's
    # direct publication limit. Recipe/checkpoint ancestry stays transitive.
    for fpga,record in summary['native_word_observations'].items():
        for number in range(40):
            path=source.root/(fpga+f'-recipe-{number}.tcl');path.write_text('# frozen recipe\n')
            parent=bind_store_source(path,'physical-input-document-v1')
            record['observation']['source_generations'].append({'path':str(path),'schema':parent.schema,'generation':parent.generation})
    monkeypatch.setattr('emuflow.runtime.aggregate_qor',lambda runtime,*args,system_timing_result:
        {'status':'incomplete','timing':system_timing_result,'physical':{'status':'pass'}})
    from types import SimpleNamespace
    runtime={'design':'tiny'};platform=SimpleNamespace(name='board');output=source.root/'phase7c'
    report=run_native_word_phase7c(summary,runtime,platform,[{}]*4,source.schedule_path,output,
        global_sta_executable=os.environ['EMUFLOW_TEST_OPENSTA'])
    qor=json.loads((output/'qor_report.json').read_text());timing=qor['timing']
    assert report['status']=='incomplete'and report['original_members']['status']=='pending-source-index'
    assert timing['summary']['raw_datapath_maximum_ns']is timing['summary']['endpoint_tns_ns']is None
    assert timing['summary']['required_dut_period_ns']>0
    assert timing['target_clock']['period_ns']==100. and timing['target_clock']['frequency_mhz']==10.
    assert qor['runtime']['tdm_period_ns']==5. and qor['runtime']['initial_dut_frequency_mhz']==10.
    assert json.loads((output/'implementation_clocks.json').read_text())==qor['runtime']
    assert json.loads((output/'runtime_contract.json').read_text())==runtime
    assert not any('virtual' in key or 'frame' in key for key in qor['runtime'])
    assert timing['global_opensta']['status']=='verified'and timing['global_opensta']['execution']=='standalone'
    if not transit:
        assert timing['summary']['maximum_sampling_cost_ns']==48.
        assert timing['summary']['inferred_dut_frequency_mhz']==pytest.approx(1000./48.)
    else:assert timing['summary']['maximum_sampling_cost_ns']>48.
    monkeypatch.setattr('emuflow.global_sta.run_event_checks',lambda *a,**kw:pytest.fail('saved witness replay launched OpenSTA'))
    replay=source.root/'replay'
    run_native_word_phase7c(summary,runtime,platform,[{}]*4,source.schedule_path,replay,
        global_sta_results_dir=output/'global-opensta')
    assert json.loads((replay/'qor_report.json').read_text())==qor


def test_default_phase7c_dispatch_does_not_request_legacy_pin_timing_databases(tmp_path,monkeypatch):
    import emuflow.phase7c as entry
    from types import SimpleNamespace
    class Summary(dict):
        closed=False
        def close(self):self.closed=True
    summary=Summary(native_word_observations={'fpga0':{}})
    monkeypatch.setattr(entry,'validate_phase5_generation',lambda *a,**kw:{'status':'legacy'})
    monkeypatch.setattr(entry,'read_tdm_schedule',lambda *a:{})
    monkeypatch.setattr(entry.Platform,'load',lambda *a:SimpleNamespace(name='board'))
    monkeypatch.setattr(entry,'build_virtual_runtime',lambda *a:{'design':'tiny'})
    monkeypatch.setattr(entry,'validate_virtual_runtime',lambda *a:{'status':'pass'})
    monkeypatch.setattr(entry,'read_physical_summary',lambda *a:summary)
    monkeypatch.setattr(entry,'read_json',lambda *a:{'clock_periods':{'clk':10.}})
    monkeypatch.setattr(entry,'read_system_routes',lambda *a:{})
    monkeypatch.setattr(entry,'validate_cross_layer_timing_contract',lambda *a:None)
    monkeypatch.setattr(entry,'build_cross_layer_physical_binding',lambda *a:pytest.fail('legacy pin binding was requested'))
    (tmp_path/'cross_layer_timing.json').write_text('{}\n');routes=tmp_path/'routes.json';routes.write_text('{}\n')
    received={}
    def native(*args,**kwargs):received.update(kwargs);return {'status':'incomplete','native':True}
    monkeypatch.setattr('emuflow.native_word_phase7c.run_native_word_phase7c',native)
    source=tmp_path/'original.sqlite';paths=tmp_path/'original-paths.json'
    report=entry.run_phase7c(*(tmp_path/n for n in('schedule','board','phase3','phase4','phase5','phase6')),
        tmp_path/'out',physical_summary_path=tmp_path/'physical',routes_path=routes,
        original_ir_path=source,original_path_database_path=paths)
    assert report['native']and summary.closed
    assert received['source_ir']==source and received['path_database']==paths
