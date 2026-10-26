#!/usr/bin/env python3
"""Exercise indexed Phase2 clock budgets with native rising/falling FF models.

This synthetic timing/API gate is not a whole-design or physical result.
"""
import argparse
import json
import math
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import file_sha256,read_json,write_json
from emuflow.opensta import run_opensta_path_database
from emuflow.sta import iter_sta_path_database_paths,iter_selected_sta_path_database_paths,validate_sta_path_database
from emuflow.sta_clock_budgets import SCHEMA,FORMAT,validate_clock_budget
from emuflow.yosys import import_yosys_json


def prepare(root):
    ports={name:{'direction':direction,'bits':[bit]}for name,direction,bit in (
        ('clk','input',2),('enable','input',9),('q_pos','output',4),('q_neg','output',5),('q_from_neg','output',7),('raw_out','output',3))}
    # A non-unate abstraction of this AND gate invents falling clock edges
    # even for positive FFs on the same gated clock. The real engine must use
    # the instance INIT, rather than merely the FF's IS_C_INVERTED parameter.
    cells={'clock_gate': {'type':'LUT2','parameters':{'INIT':'1000'},'attributes':{},
        'port_directions':{'I0':'input','I1':'input','O':'output'},
        'connections':{'I0':[2],'I1':[9],'O':[8]}}}
    for name,data,out,inverted in (('launch_pos',4,3,False),('capture_pos',3,4,False),
        ('capture_neg',3,5,True),('launch_neg',7,6,True),('capture_from_neg',6,7,False)):
        cells[name]={'type':'FDRE','parameters':{'IS_C_INVERTED':'1'if inverted else'0'},'attributes':{},
            'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'},
            'connections':{'C':[8],'D':[data],'Q':[out],'CE':['1'],'R':['0']}}
    source=root/'fixture.json';source.write_text(json.dumps({'modules':{'clock_budget_fixture':{
        'attributes':{'top':'1'},'ports':ports,'cells':cells,'netnames':{}}}}))
    ir=root/'fixture.emuir.sqlite';import_yosys_json(source,clocks=['clk']).save(ir)
    return ir


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path);parser.add_argument('--sta',required=True)
    parser.add_argument('--architecture-timing-db',required=True,type=Path)
    args=parser.parse_args();root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    ir=prepare(root);out=root/'paths.json'
    # The production indexed path must not expand the complete EmuIR to Python.
    with patch('emuflow.opensta.EmuIR.load',side_effect=AssertionError('whole EmuIR load forbidden')):
        report=run_opensta_path_database(ir,out,clocks={'clk':10.},executable=args.sta,max_paths=100,
            architecture_timing_db_path=args.architecture_timing_db.resolve(),_stream_output=True,
            clock_budget_policy='engine-edge-v1',log_path=root/'opensta.log',
            timing_io={'input_groups':[],'output_groups':[{'clock':'clk','delay_ns':2.,'ports':['raw_out']}]})
    manifest=read_json(out)
    if manifest['schema']!=SCHEMA or manifest['payloads']['paths']['format']!=FORMAT:
        raise ValidationError('indexed Phase2 did not retain its clock-budget format')
    if manifest['source'].get('clock_logic_policy')!='indexed-init-clock-sense-v1':
        raise ValidationError('indexed Phase2 did not bind its clock-cone policy')
    expected={'launch_pos':1.,'capture_pos':1.,'capture_neg':.5,'launch_neg':.5,'capture_from_neg':.5,'raw_out':1.}
    rows=list(iter_sta_path_database_paths(out,manifest));seen=set()
    cq=float(read_json(args.architecture_timing_db)['delays_ps']['ff_clock_to_q'])/1000.
    for row in rows:
        endpoint=row['endpoint'];name=endpoint['instance']or endpoint['port'];seen.add(name)
        budget=validate_clock_budget(row)
        if name not in expected or not math.isclose(budget.target_period_coefficient,expected[name],abs_tol=1e-6):
            raise ValidationError('Phase2 changed an original clock-edge window')
        if not math.isclose(row['fixed_delay_ns'],cq,abs_tol=1e-5):
            raise ValidationError('Phase2 retained an absolute launch phase in its data delay')
    if seen!=set(expected)or len(rows)!=6:raise ValidationError('Phase2 clock-budget original-path coverage differs')
    selected=list(iter_selected_sta_path_database_paths(out,manifest,[r['id']for r in rows[::2]]))
    if selected!=[r for r in rows if r['id']in {s['id']for s in selected}]:
        raise ValidationError('indexed clock-budget selection changed records')
    checked=validate_sta_path_database(out,ir)
    write_json(root/'qualification.json',{'status':'pass','scope':'synthetic-indexed-phase2-clock-budgets-only',
        'original_paths':len(rows),'checks':checked,'engine':manifest['source']['engine'],
        'architecture_timing_db_sha256':file_sha256(args.architecture_timing_db),
        'path_database_sha256':file_sha256(out),'report':report})


if __name__=='__main__':main()
