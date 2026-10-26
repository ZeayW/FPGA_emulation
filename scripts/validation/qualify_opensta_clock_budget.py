#!/usr/bin/env python3
"""Check real OpenSTA launch/capture cycle facts on a bounded synthetic design."""
import argparse
import os
import subprocess
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import file_sha256,write_json
from emuflow.opensta import DEFAULT_TIMING_MODEL,load_timing_model,render_opensta_liberty,require_opensta_engine


def prepare(root):
    model=load_timing_model(DEFAULT_TIMING_MODEL)
    liberty=root/'fixture.lib';liberty.write_text(render_opensta_liberty(model))
    verilog=root/'fixture.v'
    lines=['module clock_budget_fixture(input clkp, clkn, clkr, d, output raw_output, q_full, q_half, q_half_capture, q_multi, q_rational);']
    for kind,clock in (('full','clkp'),('half','clkn'),('half_capture','clkp'),('multi','clkp'),('rational','clkr')):
        capture_clock='clkn'if kind=='half_capture'else'clkp'
        lines += [f'wire data_{kind};',
            f"FDRE launch_{kind}(.C({clock}), .D(d), .Q(data_{kind}), .CE(1'b1), .R(1'b0));",
            f"FDRE capture_{kind}(.C({capture_clock}), .D(data_{kind}), .Q(q_{kind}), .CE(1'b1), .R(1'b0));"]
    lines+=['assign raw_output=data_full;','endmodule','']
    verilog.write_text('\n'.join(lines))
    return liberty,verilog,float(model['cells']['FDRE']['clock_to_q_ns'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sta',required=True)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    engine=require_opensta_engine(args.sta)
    liberty,verilog,cq=prepare(root)
    script=Path(__file__).resolve().parents[1]/'opensta/qualify_clock_budget.tcl'
    command=[args.sta,'-no_init','-exit',str(script)]
    environment={**os.environ,'EMUFLOW_BUDGET_LIBERTY':str(liberty),'EMUFLOW_BUDGET_VERILOG':str(verilog),
        'EMUFLOW_BUDGET_OUTPUT':str(root/'clock-budgets.tsv'),'EMUFLOW_BUDGET_CLOCK_TO_Q_NS':str(cq)}
    with(root/'tool.log').open('w')as log:
        result=subprocess.run(command,cwd=root,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,check=False)
    marker='EMUFLOW_CLOCK_BUDGET_QUALIFICATION status=pass cases=6'
    if result.returncode or marker not in(root/'tool.log').read_text():
        raise ValidationError('OpenSTA clock-budget qualification failed; see tool.log')
    write_json(root/'qualification.json',{'status':'pass','scope':'synthetic-engine-api-only','cases':6,
        'engine':engine,'command':command,'liberty_sha256':file_sha256(liberty),'verilog_sha256':file_sha256(verilog),
        'script_sha256':file_sha256(script),'clock_budget_script_sha256':file_sha256(script.with_name('clock_budget.tcl')),
        'budget_sha256':file_sha256(root/'clock-budgets.tsv')})


if __name__=='__main__':main()
