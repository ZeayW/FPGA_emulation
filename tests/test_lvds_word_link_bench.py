"""Exercise the qualification scoreboard with explicit ideal fixture wrappers."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.lvds_selectio import selectio_bank_recipe


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
@pytest.mark.parametrize('corrupt',(False,True))
def test_scoreboard_measures_complete_word_latency_and_rejects_corruption(tmp_path,corrupt):
    path=Path(__file__).resolve().parents[1]/'scripts/validation/qualify_lvds_word_link.py'
    spec=importlib.util.spec_from_file_location('word_link_qualification_fixture',path)
    driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
    recipes={d:selectio_bank_recipe('19',d)for d in ('tx','rx')}
    (tmp_path/'tb.sv').write_text(driver.bench(tmp_path,recipes['tx'],recipes['rx']))
    modules=[]
    for d,recipe in recipes.items():
        ports=['input wire tdm_clk,riu_clk,reset','output wire pll_locked,reset_done,bank_ready']
        if d=='tx':ports+=['input wire payload_enable','input wire [159:0]tx_words']
        else:ports+=['input wire all_plls_locked,alignment_enable','output wire [159:0]rx_words',
                     'output wire word_valid,training_error']
        physical='output'if d=='tx'else'input'
        for pair in (*recipe.data_pairs,recipe.clock_pair):
            ports.extend(f"{physical} wire {pair[p]['port']}"for p in ('p','n'))
        body=['assign pll_locked=!reset,reset_done=!reset,bank_ready=!reset;']
        if d=='rx':
            body+=['reg[159:0]pipe[0:5];integer j;integer samples=0;',
                'always @(posedge tdm_clk)begin',
                ' pipe[0]<=emuflow_word_link_tb.tx_words;',
                ' for(j=1;j<6;j=j+1)pipe[j]<=pipe[j-1];',
                ' samples<=samples+1;', 'end',
                "assign word_valid=!reset,training_error=1'b0;",
                "assign rx_words=pipe[5]"+(" ^ (samples==125?160'd1:160'd0);"if corrupt else';')]
        modules+=['module emuflow_lvds_bank19_'+d+'('+',\n'.join(ports)+');','\n'.join(body),'endmodule']
    (tmp_path/'fixture.sv').write_text('\n'.join(modules))
    exe=tmp_path/'sim'
    compile=subprocess.run(['iverilog','-g2012','-s','emuflow_word_link_tb','-o',str(exe),
        str(tmp_path/'fixture.sv'),str(tmp_path/'tb.sv')],capture_output=True,text=True)
    assert compile.returncode==0,compile.stderr
    result=subprocess.run(['vvp',str(exe)],capture_output=True,text=True,timeout=20)
    output=tmp_path/'word-link-results.tsv'
    if corrupt:
        assert result.returncode!=0,result.stdout
        assert not output.exists()
        assert 'corrupted or missing payload word'in result.stdout
    else:
        assert result.returncode==0,result.stdout+result.stderr
        assert 'EMUFLOW_NATIVE_WORD_LINK_PASS'in result.stdout
        assert 'minimum_latency_word_cycles\t6'in output.read_text()
        assert 'maximum_latency_word_cycles\t6'in output.read_text()
