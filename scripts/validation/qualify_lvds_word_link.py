#!/usr/bin/env python3
"""Characterize a real native-IP 20-lane word link in behavioral loopback.

This exercises unmodified generated IP, reset/calibration, FIFO/application
clock wiring, held bitslip alignment, training-to-payload transition and all
160 bits. It is not routed/board/SI evidence or a fitted-delay characterization.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.lvds_selectio import selectio_bank_recipe
from emuflow.lvds_bank_phy import selectio_bank_to_systemverilog


def bench(root,tx,rx):
    def instance(recipe,direction):
        connections=['.tdm_clk(tdm_clk)','.riu_clk(riu_clk)',
            f'.reset({"reset"if direction=="tx"else"rx_reset"})',
            f'.pll_locked({direction}_pll_locked)',f'.reset_done({direction}_reset_done)',
            f'.bank_ready({direction}_ready)']
        if direction=='tx':connections+=['.payload_enable(payload_enable)','.tx_words(tx_words)']
        else:connections+=['.all_plls_locked(tx_pll_locked && rx_pll_locked)',
            '.alignment_enable(rx_reset_done && tx_ready)',
            '.rx_words(rx_words)','.word_valid(rx_valid)','.training_error(rx_error)']
        for lane,pair in enumerate(recipe.data_pairs):
            for polarity in ('p','n'):connections.append(f".{pair[polarity]['port']}(data_{polarity}[{lane}])")
        for polarity in ('p','n'):connections.append(f".{recipe.clock_pair[polarity]['port']}(forward_{polarity})")
        return f'emuflow_lvds_bank{recipe.bank}_{direction} {direction}_bank('+',\n'.join(connections)+');'
    destination=json.dumps(str(root/'word-link-results.tsv'))
    return '''`timescale 1ps/1ps
module emuflow_word_link_tb;
reg tdm_clk=0,riu_clk=0;
always #2500 tdm_clk=~tdm_clk;
always #5000 riu_clk=~riu_clk;
reg reset=1,payload_enable=0;
wire tx_ready,rx_ready,tx_pll_locked,rx_pll_locked,tx_reset_done,rx_reset_done;
wire rx_reset=reset || !tx_ready;
wire[19:0]data_p,data_n;wire forward_p,forward_n;
reg[159:0]tx_words=0;wire[159:0]rx_words;wire rx_valid,rx_error;
'''+instance(tx,'tx')+'\n'+instance(rx,'rx')+'''
reg[159:0]history[0:63];time launched[0:63];
integer prepared=0,last=-1,received=0,min_cycles=2147483647,max_cycles=0;
integer i,lane,index,matched,cycles,fd;
reg[159:0]next_word;
always @(posedge tdm_clk)begin
 if(payload_enable && rx_valid && received<64)begin
  matched=-1;
  for(index=0;index<prepared;index=index+1)
   if(launched[index]<=$time && history[index]===rx_words)matched=index;
  if(matched>=0)begin
   if(matched!=last+1)$fatal(1,"word ordering/throughput failure: previous=%0d received=%0d",last,matched);
   if(($time-launched[matched])%5000!=0)$fatal(1,"non-word-aligned capture");
   cycles=($time-launched[matched])/5000;
   if(cycles<min_cycles)min_cycles=cycles;
   if(cycles>max_cycles)max_cycles=cycles;
   last=matched;received=received+1;
   if(received==64)begin
    fd=$fopen('''+destination+''',"w");
    if(!fd)$fatal(1,"cannot publish word-link result");
    $fdisplay(fd,"metric\tvalue");
    $fdisplay(fd,"received_words\t%0d",received);
    $fdisplay(fd,"data_lanes\t20");
    $fdisplay(fd,"payload_word_bits\t160");
    $fdisplay(fd,"minimum_latency_word_cycles\t%0d",min_cycles);
    $fdisplay(fd,"maximum_latency_word_cycles\t%0d",max_cycles);
    $fclose(fd);
    $display("EMUFLOW_NATIVE_WORD_LINK_PASS words=%0d latency_cycles=%0d..%0d",received,min_cycles,max_cycles);
    $finish;
   end
  end else if(last>=0)$fatal(1,"corrupted or missing payload word after %0d",last);
 end
end
initial begin
 repeat(100)@(negedge tdm_clk);reset=0;
 wait(tx_ready);$display("TX_READY time_ps=%0t",$time);
 wait(rx_reset_done);$display("RX_RESET_DONE time_ps=%0t",$time);
 wait(rx_ready && rx_valid);$display("RX_ALIGNED time_ps=%0t",$time);
 for(i=0;i<64;i=i+1)begin
  @(negedge tdm_clk);
  for(lane=0;lane<20;lane=lane+1)next_word[8*lane+:8]=(17+7*lane+i)%251;
  history[i]=next_word;launched[i]=$time+2500;
  prepared=i+1;tx_words=next_word;payload_enable=1;
 end
 repeat(128)@(posedge tdm_clk);
 $fatal(1,"payload drain failed: received %0d/64 words; ready=%b valid=%b error=%b",received,rx_ready,rx_valid,rx_error);
end
initial begin
 #(64'd250000000);
 $fatal(1,"startup timeout TX ready/pll/reset=%b%b%b RX=%b%b%b valid/error=%b%b",tx_ready,tx_pll_locked,tx_reset_done,rx_ready,rx_pll_locked,rx_reset_done,rx_valid,rx_error);
end
endmodule
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--vivado',required=True)
    parser.add_argument('--bank',default='19')
    args=parser.parse_args()
    root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    recipes={d:selectio_bank_recipe(args.bank,d)for d in ('tx','rx')}
    for d,recipe in recipes.items():
        (root/(d+'-bank.tsv')).write_text(recipe.configuration_tsv())
        (root/(d+'-interface.sv')).write_text(selectio_bank_to_systemverilog(recipe,ip_module='emuflow_selectio_'+d))
    (root/'word-link-tb.sv').write_text(bench(root,recipes['tx'],recipes['rx']))
    script=Path(__file__).resolve().parents[1]/'vivado/qualify_selectio_word_link.tcl'
    started=time.monotonic()
    with(root/'vivado-simulation.log').open('w')as log:
        result=subprocess.run([args.vivado,'-mode','batch','-notrace','-nojournal','-nolog','-source',str(script),
            '-tclargs',recipes['tx'].part,str(root)],cwd=root,stdout=log,stderr=subprocess.STDOUT)
    summary={'status':'fail','qualification':'vendor-behavioral-zero-cable-word-link',
        'scope':'digital native-IP simulation; no place/route, board, SI or fitted fixed-delay proof',
        'runtime_seconds':time.monotonic()-started,'exit_code':result.returncode,
        'configuration_sha256':{d:file_sha256(root/(d+'-bank.tsv'))for d in recipes}}
    # XSim can return to Tcl at $fatal and a later `run` may resume the kernel.
    # A subsequently written success file cannot erase that earlier failure.
    failure_marker=None
    with(root/'vivado-simulation.log').open()as log:
        for line in log:
            if line.lstrip().startswith(('Fatal:','ERROR:')):
                failure_marker=line.strip()[:1024];break
    if failure_marker is not None:summary['simulation_failure_marker']=failure_marker
    measurement=root/'word-link-results.tsv'
    if result.returncode==0 and failure_marker is None and measurement.is_file():
        if measurement.stat().st_size>4096:raise ValidationError('native word-link result exceeds scalar bound')
        rows=measurement.read_text().splitlines()
        if len(rows)!=6 or rows[0]!='metric\tvalue':raise ValidationError('native word-link result header/count is invalid')
        values={k:int(v)for k,v in (line.split('\t')for line in rows[1:])}
        if (set(values)!={'received_words','data_lanes','payload_word_bits','minimum_latency_word_cycles','maximum_latency_word_cycles'}
            or values['received_words']!=64 or values['data_lanes']!=20 or values['payload_word_bits']!=160
            or not 0<values['minimum_latency_word_cycles']<=values['maximum_latency_word_cycles']):
            raise ValidationError('native word-link result is invalid')
        summary.update(status='pass',measurements=values,
            declared_30ns_capture_window_feasible_in_this_simulation=values['maximum_latency_word_cycles']<=6,
            fixed_30ns_word_arc_matches_in_this_simulation=(values['minimum_latency_word_cycles']==6
                and values['maximum_latency_word_cycles']==6),
            result_sha256=file_sha256(measurement))
    (root/'qualification.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,sort_keys=True))
    raise SystemExit(0 if summary['status']=='pass'else 1)


if __name__=='__main__':main()
