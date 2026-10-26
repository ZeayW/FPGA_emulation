#!/usr/bin/env python3
"""Read complete launch tags from real OpenSTA without a selected path list."""
import argparse
import csv
from copy import deepcopy
import math
import os
from pathlib import Path
import subprocess

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.opensta import DEFAULT_TIMING_MODEL, load_timing_model, render_opensta_liberty, require_opensta_engine


def prepare(root):
    model=load_timing_model(DEFAULT_TIMING_MODEL)
    negative=deepcopy(model['cells']['FDRE']);negative['kind']='falling_edge_ff'
    model['cells']['NEG']=negative
    model['cells']['ANDCLK']={'kind':'combinational','inputs':['I0','I1'],'outputs':['O'],
        'delay_ns':.1,'timing_sense':{'O':{'I0':'positive_unate','I1':'positive_unate'}}}
    model['cells']['INVCLK']={'kind':'combinational','inputs':['I'],'outputs':['O'],
        'delay_ns':.1,'timing_sense':{'O':{'I':'negative_unate'}}}
    text=render_opensta_liberty(model)
    # Two sequential clock-to-output arcs model a shared hard-macro output.
    # This fixture checks tag enumeration, not any device macro's behavior.
    macro='''
  cell (DUAL) {
    area : 1;
    ff (IQA, IQNA) { clocked_on : "C"; next_state : "D"; }
    ff (IQB, IQNB) { clocked_on : "K"; next_state : "D"; }
    pin (C) { direction : input; clock : true; }
    pin (K) { direction : input; clock : true; }
    pin (D) { direction : input; }
    pin (Q) { direction : output; function : "IQA | IQB";
      timing () { related_pin : "C"; timing_type : rising_edge;
        cell_rise (scalar) { values ("0.12"); } cell_fall (scalar) { values ("0.12"); }
      }
      timing () { related_pin : "K"; timing_type : rising_edge;
        cell_rise (scalar) { values ("0.12"); } cell_fall (scalar) { values ("0.12"); }
      }
    }
  }
'''
    end=text.rfind('}')
    liberty=root/'fixture.lib';liberty.write_text(text[:end]+macro+text[end:])
    verilog=root/'fixture.v'
    lines=['module launch_clock_fixture(input clk, other_clk, unknown_clk, data, output [6:0] q, output unclocked_output, buffered);',
           "wire gate_clock, inverted_clock;",
           "ANDCLK gate(.I0(clk), .I1(1'b1), .O(gate_clock));",
           "INVCLK inverter(.I(clk), .O(inverted_clock));"]
    for index,(name,cell,clock) in enumerate([('positive','FDRE','clk'),('negative','NEG','clk'),
        ('gated','FDRE','gate_clock'),('inverted','FDRE','inverted_clock'),
        ('unselected','FDRE','clk'),('shifted','FDRE','other_clk')]):
        lines.append(f"{cell} {name}(.C({clock}), .D(data), .Q(q[{index}]), .CE(1'b1), .R(1'b0));")
    lines.extend(["DUAL dual(.C(clk), .K(other_clk), .D(data), .Q(q[6]));",
                  "FDRE unclocked(.C(unknown_clk), .D(data), .Q(unclocked_output), .CE(1'b1), .R(1'b0));",
                  "INVCLK buffer(.I(q[0]), .O(buffered));",'endmodule',''])
    verilog.write_text('\n'.join(lines))
    return liberty,verilog


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sta',required=True)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    engine=require_opensta_engine(args.sta)
    liberty,verilog=prepare(root)
    script=Path(__file__).resolve().parents[1]/'opensta/qualify_launch_clock_edges.tcl'
    output=root/'launch-clocks.tsv'
    environment={**os.environ,'EMUFLOW_LAUNCH_LIBERTY':str(liberty),
        'EMUFLOW_LAUNCH_VERILOG':str(verilog),'EMUFLOW_LAUNCH_OUTPUT':str(output)}
    command=[args.sta,'-no_init','-exit',str(script)]
    with(root/'tool.log').open('w')as log:
        completed=subprocess.run(command,cwd=root,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
    if completed.returncode or 'EMUFLOW_LAUNCH_CLOCK_QUALIFICATION status=pass pins=8 rejected=3'not in(root/'tool.log').read_text():
        raise ValidationError('OpenSTA launch-clock qualification failed; see tool.log')
    if not output.is_file()or output.stat().st_size>65536:
        raise ValidationError('launch-clock qualification output is absent or exceeds bound')
    expected={'positive/Q':{('base',0.)},'negative/Q':{('base',5.)},'gated/Q':{('base',0.)},
        'inverted/Q':{('base',5.)},'unselected/Q':{('base',0.)},'shifted/Q':{('shifted',2.5)},
        'dual/Q':{('base',0.),('shifted',2.5)},'data':{('base',0.)}}
    observed={}
    with output.open()as stream:
        reader=csv.DictReader(stream,delimiter='\t')
        if reader.fieldnames!=['pin_hex','clock_hex','period_ns','phase_ns']:
            raise ValidationError('launch-clock output header differs')
        for row in reader:
            pin=bytes.fromhex(row['pin_hex']).decode();clock=bytes.fromhex(row['clock_hex']).decode()
            period,phase=float(row['period_ns']),float(row['phase_ns'])
            if not math.isfinite(phase)or not math.isclose(period,10.,abs_tol=1e-5):
                raise ValidationError('launch-clock period/phase differs')
            fact=(clock,phase)
            if fact in observed.setdefault(pin,set()):raise ValidationError('launch-clock fact repeats')
            observed[pin].add(fact)
    if set(observed)!=set(expected)or any(len(observed[pin])!=len(expected[pin])or any(
        not any(clock==expected_clock and math.isclose(phase,expected_phase,rel_tol=0,abs_tol=1e-5)
                for expected_clock,expected_phase in expected[pin])for clock,phase in observed[pin])for pin in expected):
        raise ValidationError('launch-clock population or phase differs: '+repr(observed))
    write_json(root/'qualification.json',{'status':'pass','scope':'synthetic-engine-api-only',
        'pins':len(expected),'clock_facts':sum(map(len,expected.values())),'rejected_queries':3,
        'selected_path_enumeration':False,'engine':engine,'command':command,
        'liberty_sha256':file_sha256(liberty),'verilog_sha256':file_sha256(verilog),
        'script_sha256':file_sha256(script),'helper_sha256':file_sha256(script.with_name('launch_clock_edges.tcl')),
        'output_sha256':file_sha256(output)})


if __name__=='__main__':main()
