"""Independent serial streams exercise every bit phase and every payload byte."""
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.lvds_word_aligner import word_aligner_to_systemverilog
from emuflow.lvds_selectio import selectio_bank_recipe
from emuflow.lvds_bank_phy import selectio_core_ports,selectio_bank_to_systemverilog


def serial_words(words,phase):
    bits=[(word>>bit)&1 for word in words for bit in range(8)]
    return [sum(bits[start+bit]<<bit for bit in range(8))
            for start in range(phase,len(bits)-7,8)]


def simulate(tmp_path,bench):
    (tmp_path/'aligner.sv').write_text(word_aligner_to_systemverilog())
    (tmp_path/'tb.sv').write_text(bench)
    result=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(tmp_path/'sim'),
        str(tmp_path/'aligner.sv'),str(tmp_path/'tb.sv')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return subprocess.run(['vvp',str(tmp_path/'sim')],cwd=tmp_path,capture_output=True,text=True,timeout=20)


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
@pytest.mark.parametrize('phase',range(8))
@pytest.mark.parametrize('stages',(1,2))
def test_all_serial_phases_bytes_fixed_pipeline_age_and_reset_retraining(tmp_path,phase,stages):
    # Bits originate in a serialized transmit stream, independently of the
    # aligner's cross-word barrel-shift implementation.
    words=[0x2c]*48+list(range(256))+[0x2c]*8
    for epoch,offset in enumerate((phase,7-phase)):
        raw=serial_words(words,offset)
        (tmp_path/f'raw{epoch}.hex').write_text(''.join(f'{word:02x}\n'for word in raw[:310]))
    (tmp_path/'expected.hex').write_text(''.join(f'{word:02x}\n'for word in words))
    bench='''`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1,enable=0,raw_valid=1;reg[7:0]raw_word=0;
wire[7:0]out;wire aligned,valid,error;
reg[7:0]raw[0:309],expected[0:311];integer epoch,i;
emuflow_lvds_word_align #(.OUTPUT_STAGES(@STAGES@)) dut(clk,reset,enable,raw_valid,raw_word,out,aligned,valid,error);
initial begin
 $readmemh("expected.hex",expected);
 for(epoch=0;epoch<2;epoch=epoch+1)begin
  if(epoch==0)$readmemh("raw0.hex",raw);else $readmemh("raw1.hex",raw);
  repeat(3)@(negedge clk);reset=0;enable=1;raw_valid=1;
  for(i=0;i<310;i=i+1)begin
   @(negedge clk);raw_word=raw[i];
   @(posedge clk);
   if(i>=50)begin
    if(!aligned||!valid||error)$fatal(1,"not aligned at epoch/index %0d %0d",epoch,i);
    if(out!==expected[i-@STAGES@])$fatal(1,"wrong bit order/word age epoch=%0d index=%0d got=%h expected=%h",epoch,i,out,expected[i-@STAGES@]);
   end
  end
  @(negedge clk);raw_valid=0;
  @(posedge clk);#0.1;
  if(!error||aligned||valid)$fatal(1,"admitted FIFO loss did not invalidate output");
  repeat(3)@(negedge clk);raw_valid=1;
  if(!error)$fatal(1,"FIFO-loss error must remain latched");
  reset=1;enable=0;
  @(posedge clk);#0.1;
  if(error||aligned||valid)$fatal(1,"reset did not clear training state");
 end
 $display("SERIAL_PHASE_BYTES_RETRAIN_PASS");$finish;
end
initial begin #10000;$fatal(1,"test timeout");end
endmodule
'''.replace('@STAGES@',str(stages))
    result=simulate(tmp_path,bench)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'SERIAL_PHASE_BYTES_RETRAIN_PASS'in result.stdout


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
def test_nontraining_input_expires_without_payload_admission(tmp_path):
    result=simulate(tmp_path,'''`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1;wire[7:0]out;wire aligned,valid,error;
emuflow_lvds_word_align dut(clk,reset,1'b1,1'b1,8'h00,out,aligned,valid,error);
initial begin
 repeat(2)@(negedge clk);reset=0;
 repeat(515)begin @(posedge clk);if(aligned||valid)$fatal(1,"nontraining data admitted");end
 #0.1;if(!error)$fatal(1,"training never expired");
 $display("TRAINING_TIMEOUT_PASS");$finish;
end
endmodule
''')
    assert result.returncode==0,result.stdout+result.stderr
    assert 'TRAINING_TIMEOUT_PASS'in result.stdout


@pytest.mark.parametrize('policy',('word-two-stage-v1','word-one-stage-v1'))
def test_explicit_raw_rx_recipe_has_complete_distinct_interface(policy):
    recipe=selectio_bank_recipe('19','rx',rx_alignment=policy)
    assert dict(recipe.settings)['ENABLE_BITSLIP']=='0'
    ports=selectio_core_ports(recipe)
    assert sum(width for direction,width in ports.values())==476
    assert 'start_bitslip'not in ports and not any(name.startswith('bitslip_error_')for name in ports)
    rtl=selectio_bank_to_systemverilog(recipe,ip_module='native_raw')
    assert rtl.count('module emuflow_lvds_word_align #(')==1
    assert all(f' lane_{lane} ('in rtl for lane in range(20))
    separate=selectio_bank_to_systemverilog(recipe,ip_module='native_raw',include_common=False)
    assert 'module emuflow_lvds_word_align #('not in separate


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
@pytest.mark.parametrize('policy',('word-two-stage-v1','word-one-stage-v1'))
def test_raw_bank_wrapper_trains_all_twenty_lanes_and_gates_fifo_loss(tmp_path,policy):
    recipe=selectio_bank_recipe('19','rx',rx_alignment=policy)
    ports=selectio_core_ports(recipe);declarations=[];assignments=[]
    words={pair['word_port']:lane+17 for lane,pair in enumerate(recipe.data_pairs)}
    for name,(direction,width)in ports.items():
        declarations.append(f"{'input'if direction=='IN'else'output'} wire "+(f'[{width-1}:0] 'if width>1 else'')+name)
        if direction=='OUT':
            value="1'b1"if name.startswith(('vtc_rdy','dly_rdy'))or name in {'pll0_locked','intf_rdy'}else"'0"
            if name=='rst_seq_done':value='!rst'
            if name=='fifo_rd_data_valid':value='tb.raw_valid'
            if name in words:value=f"(tb.payload ? 8'd{words[name]} : 8'h2c)"
            assignments.append(f'assign {name}={value};')
    native='module native_raw('+','.join(declarations)+');\n'+'\n'.join(assignments)+'\nendmodule\n'
    (tmp_path/'bank.sv').write_text(native+selectio_bank_to_systemverilog(recipe,ip_module='native_raw',module='bank'))
    connections=['.tdm_clk(clk)','.riu_clk(clk)','.reset(reset)',".all_plls_locked(1'b1)",
        ".alignment_enable(1'b1)",'.bank_ready(ready)','.rx_words(words)','.word_valid(valid)','.training_error(error)']
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        connections.extend(f".{pair[p]['port']}(1'b0)"for p in ('p','n'))
    bench='''`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1,payload=0,raw_valid=1;
wire[159:0]words;wire ready,valid,error;integer lane;
bank dut('''+','.join(connections)+''');
initial begin
 repeat(3)@(negedge clk);reset=0;
 wait(ready&&valid);@(negedge clk);payload=1;
 repeat(3)@(posedge clk);#0.1;
 for(lane=0;lane<20;lane=lane+1)
  if(words[8*lane+:8]!==lane+17)$fatal(1,"bank payload lane order");
 @(negedge clk);raw_valid=0;@(posedge clk);#0.1;
 if(ready||valid||!error)$fatal(1,"bank admitted FIFO loss");
 $display("RAW_BANK_TRAIN_PAYLOAD_PASS");$finish;
end
initial begin #5000;$fatal(1,"bank startup timeout");end
endmodule
'''
    (tmp_path/'tb.sv').write_text(bench)
    result=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(tmp_path/'sim'),
        str(tmp_path/'bank.sv'),str(tmp_path/'tb.sv')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    result=subprocess.run(['vvp',str(tmp_path/'sim')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'RAW_BANK_TRAIN_PAYLOAD_PASS'in result.stdout
