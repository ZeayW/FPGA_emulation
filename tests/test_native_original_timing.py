"""All-original setup windows and physical capture checks from indexed owners."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from tests.native_build import original_timing_test

PERIOD=10.000000222044612


@pytest.fixture
def original(tmp_path):
    payload=tmp_path/'paths.sqlite';selector=tmp_path/'paths.json';control=tmp_path/'control.json'
    # These are current producer columns; no ordered-net rows are needed by
    # this consumer, which must never expand the original timing graph.
    with closing(sqlite3.connect(payload))as db,db:
        db.executescript('''PRAGMA user_version=2;
          CREATE TABLE paths(ordinal INTEGER PRIMARY KEY,path_id TEXT NOT NULL,clock_domain TEXT,clock_period_ns REAL,
            slack_ns REAL,fixed_delay_ns REAL,start_object TEXT,start_instance TEXT,start_port TEXT,start_bit INTEGER,
            end_object TEXT,end_instance TEXT,end_port TEXT,end_bit INTEGER);
          CREATE UNIQUE INDEX path_identity ON paths(path_id);
          CREATE TABLE path_nets(path_ordinal INTEGER,net_ordinal INTEGER,net_id TEXT);
          CREATE TABLE clock_budgets(path_ordinal INTEGER PRIMARY KEY,source_clock TEXT,source_period_ns REAL,
            launch_edge_ns REAL,capture_edge_ns REAL,source_offset_ns REAL,target_cycle_time_ns REAL,
            multicycle_adjustment_ns REAL,check_margin_ns REAL,source_latency_ns REAL,target_clock_delay_ns REAL,
            clock_skew_ns REAL,output_delay INTEGER,path_delay INTEGER,latch_check INTEGER);''')
        cases=[('full','clk',0.,0.,0.,PERIOD,0.,0),
               ('half-launch','clk',PERIOD/2,0.,0.,PERIOD,0.,0),
               ('half-capture','clk',0.,PERIOD/2,0.,PERIOD/2,0.,0),
               ('multicycle','clk',0.,0.,0.,PERIOD,PERIOD,0),
               ('rational','rational',0.,0.,PERIOD*.9,PERIOD*.1,0.,0),
               ('output','clk',0.,0.,0.,PERIOD,0.,1)]
        for i,(name,clock,launch,capture,offset,target,mcp,output)in enumerate(cases):
            db.execute('INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (i,name,'clk',10.,0.,0.,'source/Q','source','Q',0,'data_out'if output else'capture/D',
                 None if output else'capture','data_out'if output else'D',0))
            db.execute('INSERT INTO clock_budgets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (i,clock,PERIOD*.9 if clock=='rational'else PERIOD,launch,capture,offset,target,mcp,87.,2.,3.,1.,output,0,0))
    descriptor=dict(schema='emuflow.sta-path-database/v4',payloads={'paths':dict(format='sqlite-sta-path/v2',
        path=payload.name,records=6,sha256=hashlib.sha256(payload.read_bytes()).hexdigest())},summary=dict(paths=6,clock_budget_paths=6),
        source=dict(clock_budget_policy='engine-edge-v1',clock_logic_policy='indexed-init-clock-sense-v1'))
    selector.write_text(json.dumps(descriptor))
    config=dict(clocks={'clk':dict(declared_period_ns=10.,engine_period_ns=PERIOD),
                        'rational':dict(declared_period_ns=9.,engine_period_ns=PERIOD*.9)},
                targets={'clk':100.,'rational':90.},event_offset_ns=24.,data_delay_ns=4.,sampling_cost_ns=6.)
    control.write_text(json.dumps(config))
    return selector,payload,control


def execute(original,mode='windows',seal=None):
    selector,payload,control=original
    seal=seal or hashlib.sha256(selector.read_bytes()).hexdigest()
    return subprocess.run([str(original_timing_test()),str(selector),str(payload),seal,str(control),mode],capture_output=True,text=True)


def change_json(path,change):
    value=json.loads(path.read_text());change(value);path.write_text(json.dumps(value))


def test_raw_engine_periods_keep_exact_full_half_and_multicycle_windows(original):
    result=execute(original);assert result.returncode==0,result.stderr
    rows={row[0]:row[1:]for row in json.loads(result.stdout)}
    assert rows['full']==[0.,100.,1.,24.,4.,98.,87.]
    assert rows['half-launch']==[50.,50.,.5,74.,4.,98.,87.]
    assert rows['half-capture']==[0.,50.,.5,24.,4.,48.,87.]
    assert rows['multicycle']==[0.,200.,2.,24.,4.,198.,87.]
    assert rows['rational']==pytest.approx([0.,10.,.1,24.,4.,8.,87.])
    assert rows['output']==rows['full']
    # A rounded displayed period would place the full/half deadline slightly
    # after the exact word boundary. No tolerance snap repairs that mistake.
    assert PERIOD/10.*100.>rows['full'][1]


def test_signed_physical_margin_is_applied_once_not_reference_library_margin(original):
    change_json(original[2],lambda d:d.update(data_delay_ns=4.,sampling_cost_ns=-2.))
    result=execute(original);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)[0][6]==106.


@pytest.mark.parametrize('damage',['source-raw-period','target-displayed-period','missing-root','missing-target',
    'nonuniform','no-engine-period','nonfinite-target','negative-data','negative-window','latch','absolute-delay'])
def test_incomplete_or_incompatible_clock_and_physical_contexts_fail(original,damage):
    if damage in {'source-raw-period','target-displayed-period','negative-window','latch','absolute-delay'}:
        column,value={'source-raw-period':('source_period_ns',10.),'target-displayed-period':('clock_period_ns',20.),
            'negative-window':('target_cycle_time_ns',0.),'latch':('latch_check',1),'absolute-delay':('path_delay',1)}[damage]
        with sqlite3.connect(original[1])as db:
            db.execute(f'UPDATE {"paths"if column=="clock_period_ns"else"clock_budgets"} SET {column}=? WHERE {"ordinal"if column=="clock_period_ns"else"path_ordinal"}=0',(value,))
    else:
        def alter(d):
            if damage=='missing-root':d['clocks'].pop('rational')
            elif damage=='missing-target':d['targets'].pop('rational')
            elif damage=='nonuniform':d['targets']['rational']=100.
            elif damage=='no-engine-period':d['clocks']['clk'].pop('engine_period_ns')
            elif damage=='nonfinite-target':d['targets']['clk']='inf'
            else:d['data_delay_ns']=-1.
        change_json(original[2],alter)
    result=execute(original);assert result.returncode!=0 and not result.stdout


@pytest.mark.parametrize('damage',['missing-budget','orphan-budget','missing-endpoint','ordinal-gap','numeric-text',
    'no-unique-identity','partial-unique-index','duplicate-identity','selector-count','budget-count',
    'legacy-policy','missing-clock-sense','wrong-schema','wrong-payload','parent-payload','seal'])
def test_all_original_coverage_and_sealed_owner_relations_are_required(original,damage):
    if damage in {'selector-count','budget-count','legacy-policy','missing-clock-sense','wrong-schema','wrong-payload','parent-payload'}:
        def alter(d):
            if damage=='selector-count':d['payloads']['paths']['records']=5
            elif damage=='budget-count':d['summary']['clock_budget_paths']=5
            elif damage=='legacy-policy':d['source']['clock_budget_policy']='legacy-absolute-arrival-v1'
            elif damage=='missing-clock-sense':d['source'].pop('clock_logic_policy')
            elif damage=='wrong-schema':d['schema']='emuflow.sta-path-database/v3'
            elif damage=='wrong-payload':d['payloads']['paths']['path']='absent.sqlite'
            else:d['payloads']['paths']['path']='../paths.sqlite'
        change_json(original[0],alter)
    elif damage!='seal':
        with sqlite3.connect(original[1])as db:
            if damage=='missing-budget':db.execute('DELETE FROM clock_budgets WHERE path_ordinal=0')
            elif damage=='orphan-budget':db.execute('INSERT INTO clock_budgets SELECT 100,source_clock,source_period_ns,launch_edge_ns,capture_edge_ns,source_offset_ns,target_cycle_time_ns,multicycle_adjustment_ns,check_margin_ns,source_latency_ns,target_clock_delay_ns,clock_skew_ns,output_delay,path_delay,latch_check FROM clock_budgets WHERE path_ordinal=0')
            elif damage=='missing-endpoint':db.execute('UPDATE paths SET start_port=NULL WHERE ordinal=0')
            elif damage=='ordinal-gap':db.execute('UPDATE paths SET ordinal=100 WHERE ordinal=0')
            elif damage=='numeric-text':db.execute("UPDATE clock_budgets SET launch_edge_ns='corrupt' WHERE path_ordinal=0")
            else:
                db.execute('DROP INDEX path_identity')
                if damage=='partial-unique-index':db.execute('CREATE UNIQUE INDEX partial_identity ON paths(path_id) WHERE ordinal=0')
                elif damage=='duplicate-identity':db.execute("UPDATE paths SET path_id='full' WHERE ordinal=1")
    result=execute(original,seal='0'*64 if damage=='seal'else None)
    assert result.returncode!=0 and not result.stdout


def test_sealed_selector_and_payload_relocate_together(original,tmp_path):
    moved=tmp_path/'moved';moved.mkdir()
    copies=tuple(moved/p.name for p in original)
    for src,dst in zip(original,copies):shutil.copyfile(src,dst)
    assert execute(copies).stdout==execute(original).stdout


def test_selector_generation_is_held_through_complete_iteration(original):
    result=execute(original,'mutate')
    assert result.returncode!=0 and not result.stdout
