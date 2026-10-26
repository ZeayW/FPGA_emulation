"""Mapped physical primitive buses must use their library-declared widths."""
import json
import os
from pathlib import Path
import subprocess
from contextlib import nullcontext

import pytest

from emuflow.synthesis import build_yosys_script
from emuflow.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


pytestmark = pytest.mark.skipif(not os.environ.get('EMUFLOW_YOSYS_TEST_EXECUTABLE'),
                               reason='explicit qualified Yosys required')


def run_script(root, script, *, stdout=None):
    with (root/'yosys.log').open('w') as log, (stdout.open('w')if stdout else nullcontext(log)) as output:
        flags=['-q']if stdout else['-Q','-T']
        result = subprocess.run([os.environ['EMUFLOW_YOSYS_TEST_EXECUTABLE'], *flags, '-p', script],
                                cwd=root, stdout=output, stderr=log)
    assert result.returncode == 0, (root/'yosys.log').read_text()[-6000:]


@pytest.mark.parametrize('kind,width,parity,enable', [('RAMB18E2',16,2,2), ('RAMB36E2',32,4,4),
                                                   ('URAM288',72,9,9)])
@pytest.mark.parametrize('nested', [False,True])
def test_post_mapping_normalizer_preserves_live_bits_and_hierarchy(tmp_path,kind,width,parity,enable,nested):
    source=tmp_path/'mapped.v'
    if kind=='URAM288':
        pins=f'.CLK(clk),.EN_A(32\'h1),.DIN_A({{{{{width}{{1\'b0}}}},din}}),.DIN_B(144\'h0),.BWE_A({{we,we}}),.DOUT_A(wide)'
        expected={'DIN_A':72,'DIN_B':72,'BWE_A':9,'DOUT_A':72,'EN_A':1}
        parameters=''
        output_width=144
    else:
        pins=f'.CLKARDCLK(clk),.CLKBWRCLK(clk),.ENARDEN(32\'h1),.DINADIN({{{{{64-width}{{1\'b0}}}},din}}),.DINBDIN(64\'h0),.DINPADINP(8\'h0),.DINPBDINP(8\'h0),.WEA({{we,we}}),.DOUTADOUT(wide)'
        expected={'DINADIN':width,'DINBDIN':width,'DINPADINP':parity,'DINPBDINP':parity,
                  'WEA':enable,'DOUTADOUT':width,'ENARDEN':1}
        parameters='#(.READ_WIDTH_A(18),.WRITE_WIDTH_A(18))'
        output_width=64
    ports=f'(input clk,input [{enable-1}:0] we,input [{width-1}:0] din,output [{width-1}:0] q)'
    body=f'module child{ports}; wire [{output_width-1}:0] wide; {kind} {parameters} ram ({pins}); assign q=wide[{width-1}:0]; endmodule\n'
    source.write_text(body+f'module top{ports}; child u(.clk(clk),.we(we),.din(din),.q(q)); endmodule\n')
    # Run the exact post-mapping commands selected by the production builder.
    production=build_yosys_script([source],top='top',output=tmp_path/'unused.json',
        mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE, hierarchical_interchange=nested,stream_json=nested)
    commands=[c.strip()for c in production.split(';')]
    end=commands.index('blackbox =A:whitebox')
    normalizer=commands[end-1]
    assert normalizer=='hierarchy -check -top top'
    before=tmp_path/'before.json';after=tmp_path/'after.json'
    run_script(tmp_path,f'read_verilog -lib +/xilinx/cells_xtra.v; read_verilog "{source}"; '
        f'write_json "{before}"; {normalizer}; blackbox =A:whitebox; '
        +(''if nested else'flatten; ')+f'write_json "{after}"')
    old=json.loads(before.read_text())['modules'];new=json.loads(after.read_text())['modules']
    a=old['child']['cells']['ram']
    z=new['child']['cells']['ram']if nested else new['top']['cells']['u.ram']
    assert a['type']==z['type']==kind
    assert a['parameters']==z['parameters']
    assert old['top']['ports']==new['top']['ports']
    for pin,size in expected.items():
        assert len(old[kind]['ports'][pin]['bits'])==size
        assert len(a['connections'][pin])>size
        assert len(z['connections'][pin])==size
        if nested:assert z['connections'][pin]==a['connections'][pin][:size]
    assert ('child'in new)==nested


@pytest.mark.parametrize('nested',[False,True])
def test_full_xilinx_mapping_exports_only_physical_ram_port_widths(tmp_path,nested):
    source=tmp_path/'memory.v'
    source.write_text('''module top(input clk,we,input [9:0] a,input [7:0] d,output reg [7:0] q);
reg [7:0] mem[0:1023];
always @(posedge clk) begin if(we) mem[a]<=d; q<=mem[a]; end
endmodule
''')
    output=tmp_path/'mapped.json'
    script=build_yosys_script([source],top='top',output=output,
        mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,hierarchical_interchange=nested,stream_json=nested)
    run_script(tmp_path,script,stdout=output if nested else None)
    modules=json.loads(output.read_text())['modules'];rams=[]
    for module in modules.values():
        for cell in module.get('cells',{}).values():
            if cell['type'] in {'RAMB18E2','RAMB36E2','URAM288'}:rams.append(cell)
    assert rams
    for cell in rams:
        ports=modules[cell['type']]['ports']
        for name,bits in cell['connections'].items():assert len(bits)==len(ports[name]['bits']),(cell['type'],name)
