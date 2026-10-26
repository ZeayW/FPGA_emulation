#!/usr/bin/env python3
"""Qualify streamed source launch tags under the shared Phase2 clock/I/O setup."""
import argparse
import csv
import math
import os
from pathlib import Path
import subprocess

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.opensta import require_opensta_engine
from qualify_opensta_launch_clocks import prepare


def qualify(sta, root):
    root.mkdir(parents=True, exist_ok=False)
    engine=require_opensta_engine(sta)
    liberty,verilog=prepare(root)
    verilog.write_text(verilog.read_text().replace('input clk, other_clk, unknown_clk, data,','input clk, other_clk, unused_clk, unknown_clk, data,'))
    requests=root/'sources.tsv';clocks=root/'clocks.tsv';io=root/'timing-io.tsv'
    names=['positive/Q','negative/Q','gated/Q','inverted/Q','unselected/Q','shifted/Q','dual/Q','data']
    requests.write_text('source_seq\tpin_hex\n'+''.join(f'{i}\t{name.encode().hex()}\n'for i,name in enumerate(names)))
    clocks.write_text('clock_hex\tperiod_ns\n'+''.join(f'{name.encode().hex()}\t10\n'for name in ('clk','other_clk','unused_clk')))
    io.write_text('direction\tclock_hex\tdelay_ns\tport_hex\ninput\t636c6b\t0\t64617461\n')
    output=root/'launch-clocks.tsv';catalog=root/'clock-periods.tsv'
    script=Path(__file__).resolve().parents[1]/'opensta/export_launch_clock_edges.tcl'
    environment={**os.environ,'EMUFLOW_STA_LIBERTY':str(liberty),'EMUFLOW_STA_VERILOG':str(verilog),
        'EMUFLOW_STA_TOP':'launch_clock_fixture','EMUFLOW_STA_CLOCKS':str(clocks),
        'EMUFLOW_STA_TIMING_IO':str(io),'EMUFLOW_LAUNCH_REQUESTS':str(requests),'EMUFLOW_LAUNCH_OUTPUT':str(output),'EMUFLOW_LAUNCH_CLOCK_CATALOG':str(catalog)}

    def run(label):
        environment['EMUFLOW_LAUNCH_OUTPUT']=str(output if label=='export'else root/f'{label}.tsv')
        with(root/f'{label}.log').open('w')as log:
            result=subprocess.run([sta,'-no_init','-exit',str(script)],cwd=root,env=environment,
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        return result.returncode,(root/f'{label}.log').read_text()

    code,log=run('export')
    marker='EMUFLOW_LAUNCH_CLOCK_EXPORT status=pass sources=8 facts=9 clocks=3'
    if code or marker not in log or output.stat().st_size>65536:
        raise ValidationError('source launch export failed; see export.log')
    with catalog.open() as stream:
        reader=csv.DictReader(stream,delimiter='\t')
        if reader.fieldnames!=['clock_seq','clock_hex','period_ns']:
            raise ValidationError('engine clock catalog header differs')
        rows=list(reader)
    if len(rows)!=3 or any(int(row['clock_seq'])!=i or bytes.fromhex(row['clock_hex']).decode()!=name or
            not math.isclose(float(row['period_ns']),10.,abs_tol=1e-5)
            for i,(row,name) in enumerate(zip(rows,('clk','other_clk','unused_clk')))):
        raise ValidationError('engine clock catalog omitted or changed a declared clock')
    # Both primary clocks have their original Phase2 zero-phase waveform;
    # negative cells and the inverted clock must still launch at half period.
    expected=[{('clk',0.)},{('clk',5.)},{('clk',0.)},{('clk',5.)},
        {('clk',0.)},{('other_clk',0.)},{('clk',0.),('other_clk',0.)},{('clk',0.)}]
    observed=[set()for _ in expected]
    with output.open()as stream:
        reader=csv.DictReader(stream,delimiter='\t')
        if reader.fieldnames!=['source_seq','clock_hex','period_ns','phase_ns']:
            raise ValidationError('source launch export header differs')
        for row in reader:
            source=int(row['source_seq']);period=float(row['period_ns']);phase=float(row['phase_ns'])
            if not 0<=source<len(expected) or not math.isfinite(phase)or not math.isclose(period,10.,abs_tol=1e-5):
                raise ValidationError('source launch export contains invalid coordinates or clocks')
            fact=(bytes.fromhex(row['clock_hex']).decode(),phase)
            if fact in observed[source]:raise ValidationError('source launch clock tag repeats')
            observed[source].add(fact)
    if any(len(a)!=len(b)or any(not any(c==d and math.isclose(x,y,rel_tol=0,abs_tol=1e-5)
        for d,y in b)for c,x in a)for a,b in zip(observed,expected)):
        raise ValidationError('source launch export lost or changed a phase/tag')
    evidence={'status':'pass','scope':'synthetic-streamed-source-clock-export-only',
        'sources':8,'clock_facts':9,'declared_clocks':3,'unused_clock_in_catalog':True,'catalog_sha256':file_sha256(catalog),'engine':engine,'output_sha256':file_sha256(output),
        'script_sha256':file_sha256(script),'context_sha256':file_sha256(script.with_name('design_context.tcl')),
        'helper_sha256':file_sha256(script.with_name('launch_clock_edges.tcl'))}
    rejected=[]
    for label,rows,error in [
        ('combinational',['0\t'+b'buffer/O'.hex()],'not an architectural startpoint'),
        ('unclocked',['0\t'+b'unclocked/Q'.hex()],'architectural launch has no clock edge'),
        ('missing-coordinate',['1\t'+b'positive/Q'.hex()],'missing, repeated or out of order'),
        ('truncated-name',['0\t'+b'positive/Q'.hex()[:-1]],'bounded hexadecimal UTF-8'),
        ('repeated-coordinate',['0\t'+b'positive/Q'.hex(),'0\t'+b'negative/Q'.hex()],'missing, repeated or out of order'),
    ]:
        requests.write_text('source_seq\tpin_hex\n'+'\n'.join(rows)+'\n')
        code,log=run(label)
        if not code or 'EMUFLOW_LAUNCH_CLOCK_EXPORT status=pass'in log or error not in log:
            raise ValidationError('invalid launch request accepted or rejected for another reason: '+label)
        rejected.append(label)
    evidence['rejected_requests']=rejected
    write_json(root/'qualification.json',evidence)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sta',required=True);parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();qualify(args.sta,args.out.resolve())
