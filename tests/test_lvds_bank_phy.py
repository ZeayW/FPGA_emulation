"""Wrapper wiring simulation with a stub; native PHY qualification is separate."""
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.lvds_selectio import selectio_bank_recipe
from emuflow.lvds_bank_phy import selectio_core_ports,selectio_bank_to_systemverilog


def test_full_bank_interfaces_match_observed_native_port_population():
    for direction,total in (('tx',241),('rx',518)):
        ports=selectio_core_ports(selectio_bank_recipe('19',direction))
        assert sum(width for _direction,width in ports.values())==total


def stub(recipe):
    ports=selectio_core_ports(recipe)
    declarations=[];assignments=[]
    for name,(direction,width)in ports.items():
        declarations.append(f"{'input'if direction=='IN'else'output'} wire "+(f'[{width-1}:0] 'if width>1 else'')+name)
        if direction=='OUT':
            value="1'b1"if name.startswith(('vtc_rdy','dly_rdy'))or name in {'pll0_locked','intf_rdy','fifo_rd_data_valid'}else"'0"
            if name=='rst_seq_done':value='!rst'
            if name=='rx_bitslip_sync_done':value='start_bitslip'
            for lane,pair in enumerate(recipe.data_pairs):
                if name==pair['word_port']:value=f"8'h{lane+17:02x}"
            assignments.append(f'assign {name}={value};')
    return 'module native_ip(\n'+',\n'.join(declarations)+'\n);\n'+'\n'.join(assignments)+'\nendmodule\n'


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
@pytest.mark.parametrize('direction',('tx','rx'))
def test_training_payload_full_word_mapping_and_receive_validity_gates(tmp_path,direction):
    recipe=selectio_bank_recipe('19',direction)
    wrapper=selectio_bank_to_systemverilog(recipe,ip_module='native_ip',module='bank')
    (tmp_path/'bank.sv').write_text(wrapper)
    (tmp_path/'native.sv').write_text(stub(recipe))
    calls=['.tdm_clk(clk)','.riu_clk(clk)','.reset(reset)','.pll_locked(pll)',
           '.reset_done(done)','.bank_ready(ready)']
    if direction=='tx':calls+=['.payload_enable(enable)','.tx_words(words)']
    else:calls+=['.all_plls_locked(1\'b1)','.alignment_enable(enable)',
                 '.rx_words(received)','.word_valid(valid)','.training_error(error)']
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        for polarity in ('p','n'):
            value="1'b0"if direction=='rx'else''
            calls.append(f".{pair[polarity]['port']}({value})")
    # Input constants use only generated pin names. No native input is omitted.
    assertions=[]
    if direction=='tx':
        for lane,pair in enumerate(recipe.data_pairs):
            assertions.append(f"if(dut.native_phy.{pair['word_port']}!==8'h2c)$fatal(1,\"training word\");")
        assertions+=['enable=1;#0.1;']
        for lane,pair in enumerate(recipe.data_pairs):
            assertions.append(f"if(dut.native_phy.{pair['word_port']}!==words[{8*lane} +: 8])$fatal(1,\"payload lane/bit order\");")
        assertions.append(f"if(dut.native_phy.{recipe.clock_pair['word_port']}!==8'h55)$fatal(1,\"forwarded clock word\");")
    else:
        assertions+=['if(valid!==0||ready!==0)$fatal(1,"untrained validity");enable=1;#0.1;',
                     'if(valid!==1||ready!==1||error!==0)$fatal(1,"trained validity");']
        for lane in range(20):assertions.append(f"if(received[{8*lane} +: 8]!==8'h{lane+17:02x})$fatal(1,\"RX word lane order\");")
        assertions+=['force dut.native_bitslip_errors[39]=1;#0.1;',
                     'if(valid!==0||ready!==0||error!==1)$fatal(1,"error validity gate");',
                     'release dut.native_bitslip_errors[39];#0.1;']
    bench='''`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1,enable=0;
reg [159:0]words;wire[159:0]received;
wire pll,done,ready,valid,error;
bank dut('''+','.join(calls)+''');
initial begin
 words=160'h0123456789abcdef112233445566778899aabbccdd;
 #0.1;if(ready!==0)$fatal(1,"reset readiness");
 reset=0;#0.1;
'''+''.join(assertions)+'''
 reset=1;#0.1;if(ready!==0)$fatal(1,"reset must remove readiness");
 $display("BANK_INTERFACE_PASS");$finish;
end
initial begin #100;$fatal(1,"timeout");end
endmodule
'''
    (tmp_path/'tb.sv').write_text(bench)
    exe=tmp_path/'simulation'
    compile=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(exe),
        *(str(tmp_path/name)for name in ('native.sv','bank.sv','tb.sv'))],capture_output=True,text=True)
    assert compile.returncode==0,compile.stderr
    result=subprocess.run(['vvp',str(exe)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'BANK_INTERFACE_PASS'in result.stdout
