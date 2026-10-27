"""Whole-bank top projection and RTL wiring; native P&R is a separate gate."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.default_platform import materialize_research_platform
from emuflow.errors import ValidationError
from emuflow.lvds_bank_phy import selectio_core_ports
from emuflow.lvds_selectio import selectio_bank_recipe,WORD_PHY_RX_ALIGNMENT
from emuflow.lvds_word_plan import build_word_phy_plan
from emuflow.platform import Platform
from emuflow.tdm_word import WORD_EVENT_POLICY
from emuflow.virtual_board import build_virtual_board,_digest
from emuflow.verilog import _identifier
from emuflow.vivado_word_top import write_vivado_word_top,SERVICE_INPUTS,SERVICE_OUTPUTS
from emuflow.yosys import import_yosys_json


def model(root):
    path=root/'platform.json'
    materialize_research_platform(path,fpga_count=2,transport_format='lvds-word-v2')
    platform=Platform.load(path);board=build_virtual_board(platform)
    link=platform.links[0];a,b=sorted(link.endpoints)
    schedule={'platform':platform.name,'design':'word-port-fixture','word_event_policy':WORD_EVENT_POLICY,
        'bank_allocations':[{'link':link.id,'from':src,'to':dst,'bank_count':9,'lane_offset':offset,
                            'lanes':1440,'wires_per_bank':20}for src,dst,offset in ((a,b,0),(b,a,1440))]}
    transports={fpga:{'fpga':fpga,'word_event_policy':WORD_EVENT_POLICY,'endpoints':[
        {'link':link.id,'peer':peer,'kind':kind,'lane':lane}
        for kind,lane in (('tx',0),('rx',1440))]}
        for fpga,peer in ((a,b),)}
    transports[b]={'fpga':b,'word_event_policy':WORD_EVENT_POLICY,'endpoints':[
        {'link':link.id,'peer':a,'kind':'rx','lane':0},{'link':link.id,'peer':a,'kind':'tx','lane':1440}]}
    return board,build_word_phy_plan(platform,board,schedule,transports)


def partition(root,node):
    ports={name:{'direction':'input','bits':[bit]}for name,bit in
        (('fabric_clk',2),('reset',3),('links_ready',4),('clk',5),('d',6))}
    ports['q']={'direction':'output','bits':[7]*8}
    next_bit=10
    for record in node['banks']:
        width=record['logical_port_width']
        bits=[7]*width if record['direction']=='tx'else list(range(next_bit,next_bit+width))
        ports[record['logical_port']]={'direction':'output'if record['direction']=='tx'else'input','bits':bits}
        next_bit+=width
    cell={'type':'FDRE','parameters':{'INIT':'0'},'attributes':{},
        'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'},
        'connections':{'C':[5],'D':[6],'Q':[7],'CE':['1'],'R':['0']}}
    source=root/'source.json';source.write_text(json.dumps({'modules':{'dut':{'attributes':{'top':'1'},
        'ports':ports,'cells':{'launch':cell},'netnames':{}}}}))
    ir=root/'partition.emuir.sqlite';import_yosys_json(source).save(ir)
    return ir,ports


@pytest.mark.parametrize('node_index',(0,1))
def test_word_top_sources_keep_full_banks_and_exact_pin_clock_slices(tmp_path,node_index):
    board,plan=model(tmp_path);node=plan['fpgas'][node_index];ir,ports=partition(tmp_path,node)
    out=tmp_path/'native';report=write_vivado_word_top(ir,out,fpga=node['fpga'],board=board,plan=plan,dut_clock_ports=('clk',))
    assert report['active_banks']==2 and report['physical_ports']==84
    assert report['data_pairs']==40 and report['forwarded_clock_pairs']==2
    assert report['physical_implementation_qualified']is False
    top=(out/'physical_top.sv').read_text();wrappers=(out/'native-word-banks.sv').read_text()
    assert 'KEEP'not in top+wrappers and 'DONT_TOUCH'not in top+wrappers
    assert report['dut_clock_buffers']==[{'port':'clk','net':'__emuflow_word_dut_clk_0','instance':'word_dut_reference_0'}]
    assert 'BUFG word_dut_reference_0' in top
    assert f'.{_identifier("clk")}(__emuflow_word_dut_clk_0)' in top
    assert wrappers.count('module emuflow_lvds_word_align #(')==1
    assert '#(.OUTPUT_STAGES(1))'in wrappers
    assert len((out/'fixed-io-query.tsv').read_text().splitlines())==43
    assert len((out/'native-word-boundaries.tsv').read_text().splitlines())==321
    assert len((out/'native-word-clocks.tsv').read_text().splitlines())==3
    assert set(report['external_services']['inputs'])==set(SERVICE_INPUTS)
    for record in node['banks']:
        assert f"[{record['logical_lsb']} +: 160]"in top
        bus=record['logical_port']
        if record['direction']=='rx':
            assert f"assign {_identifier(bus)}[{record['logical_lsb']} +: 160] = '0;"not in top
            assert top.count('assign '+_identifier(bus)+'[')==17
    with pytest.raises(ValidationError,match='fresh output files'):
        write_vivado_word_top(ir,out,fpga=node['fpga'],board=board,plan=plan)


@pytest.mark.parametrize('damage',('source','alignment','pair-order','overlap','width','part'))
def test_bad_word_physical_sources_fail_before_emission(tmp_path,damage):
    board,plan=model(tmp_path);node=plan['fpgas'][0];ir,ports=partition(tmp_path,node)
    if damage=='source':plan['source']['board_sha256']='0'*64
    elif damage=='alignment':plan['native_phy']['rx_alignment']='native-bit-slip-v1'
    elif damage=='pair-order':
        board['wires'][0]['endpoints'][0]['pair_id']='L2';plan['source']['board_sha256']=_digest(board)
    elif damage=='overlap':node['banks'].append(copy.deepcopy(node['banks'][0]))
    elif damage=='width':node['banks'][0]['logical_port_width']+=160
    elif damage=='part':board['fpgas'][0]['part']='xcvu9p-flga2104-2-e';plan['source']['board_sha256']=_digest(board)
    out=tmp_path/'native'
    with pytest.raises(ValidationError):write_vivado_word_top(ir,out,fpga=node['fpga'],board=board,plan=plan)
    assert not out.exists()


@pytest.mark.skipif(not shutil.which('iverilog')or not shutil.which('vvp'),reason='Icarus Verilog required')
def test_native_word_top_rtl_connects_all_lanes_startup_status_and_payload(tmp_path):
    board,plan=model(tmp_path);node=plan['fpgas'][0];ir,ports=partition(tmp_path,node)
    out=tmp_path/'native';write_vivado_word_top(ir,out,fpga=node['fpga'],board=board,plan=plan,dut_clock_ports=('clk',))
    partition_ports=[];partition_body=[];native_modules=[];checks=[]
    for name,p in ports.items():
        width=len(p['bits']);partition_ports.append(f"{p['direction']} wire "+(f'[{width-1}:0] 'if width>1 else'')+name)
    for bank_index,record in enumerate(node['banks']):
        recipe=selectio_bank_recipe(record['bank'],record['direction'],
            **({'rx_alignment':WORD_PHY_RX_ALIGNMENT}if record['direction']=='rx'else{}))
        bus=record['logical_port'];base=record['logical_lsb']
        if record['direction']=='tx':
            value=sum((33+lane)<<(base+8*lane)for lane in range(20))
            partition_body.append(f"assign {bus} = {record['logical_port_width']}'h{value:x};")
            for lane,pair in enumerate(recipe.data_pairs):
                checks.append(f"if(dut.native_bank{record['bank']}_tx.native_phy.{pair['word_port']}!==8'd{33+lane})$fatal(1,\"TX lane/bit mapping\");")
        else:
            partition_body.append(f'assign q = {bus}[{base} +: 8];')
            for lane in range(20):checks.append(f"if(dut.{bus}[{base+8*lane} +: 8]!==8'd{17+lane})$fatal(1,\"RX lane/bit mapping\");")
        declarations=[];assignments=[];words={p['word_port']:lane for lane,p in enumerate(recipe.data_pairs)}
        for name,(direction,width)in selectio_core_ports(recipe).items():
            declarations.append(f"{'input'if direction=='IN'else'output'} wire "+(f'[{width-1}:0] 'if width>1 else'')+name)
            if direction=='OUT':
                value="1'b1"if name.startswith(('vtc_rdy','dly_rdy'))or name in {'pll0_locked','intf_rdy','fifo_rd_data_valid'}else"'0"
                if name=='pll0_locked':value=f'tb.pll_ok[{bank_index}]'
                if name=='rst_seq_done':value='!rst'
                if name in words:value=f"(tb.payload ? 8'd{17+words[name]} : 8'h2c)"
                assignments.append(f'assign {name}={value};')
        native_modules.append(f"module {record['ip_module']} ("+','.join(declarations)+');\n'+'\n'.join(assignments)+'\nendmodule')
    (out/'partition.sv').write_text('module dut('+','.join(partition_ports)+');\n'+'\n'.join(partition_body)+'\nendmodule')
    (out/'native-stubs.sv').write_text('module BUFG(input wire I,output wire O);assign O=I;endmodule\n'+'\n'.join(native_modules))
    calls=['.fabric_clk(clk)','.clk(clk)','.d(1\'b0)','.reset(reset)',".links_ready(1'b1)",'.q(q)']
    calls+=['.__emuflow_riu_clk(clk)',".__emuflow_all_tx_ready(1'b1)",".__emuflow_all_plls_locked(1'b1)",
        ".__emuflow_alignment_enable(1'b1)",'.__emuflow_payload_enable(payload)',
        '.__emuflow_local_tx_ready(tx_ready)','.__emuflow_local_plls_locked(plls_locked)',
        '.__emuflow_local_reset_done(reset_done)',
        '.__emuflow_local_rx_ready(ready)','.__emuflow_local_rx_word_valid(valid)','.__emuflow_local_training_error(error)']
    bench='''`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1,payload=0;reg[1:0]pll_ok=2'b11;
wire[7:0]q;wire ready,valid,error,tx_ready,plls_locked,reset_done;
__emuflow_physical_top dut('''+','.join(calls)+''');
initial begin
 repeat(3)@(negedge clk);reset=0;
 wait(ready&&valid);@(negedge clk);payload=1;
 repeat(4)@(posedge clk);#0.1;
 if(q!==8'd17||error)$fatal(1,"partition RX/status binding");
 if(tx_ready!==1'b1||plls_locked!==1'b1||reset_done!==1'b1)
   $fatal(1,"top startup status outputs must be driven and ready");
'''+''.join(checks)+'''
 for(integer bank=0;bank<2;bank=bank+1)begin
   pll_ok[bank]=0;#0.1;
   if(plls_locked!==1'b0)$fatal(1,"one unlocked bank must clear PLL consensus");
   pll_ok[bank]=1;#0.1;
   if(plls_locked!==1'b1)$fatal(1,"all locked banks must assert PLL consensus");
 end
 $display("NATIVE_WORD_TOP_WIRING_PASS");$finish;
end
initial begin #5000;$fatal(1,"top training timeout");end
endmodule
'''
    (out/'tb.sv').write_text(bench)
    result=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(out/'sim'),
        *(str(out/name)for name in ('native-stubs.sv','native-word-banks.sv','partition.sv','physical_top.sv','tb.sv'))],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    result=subprocess.run(['vvp',str(out/'sim')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'NATIVE_WORD_TOP_WIRING_PASS'in result.stdout


@pytest.mark.parametrize('clock_ports', [('clk','clk'), ('missing',), ('q',), ('fabric_clk',), 'clk'])
def test_dut_clock_buffers_require_distinct_scalar_design_clocks(tmp_path,clock_ports):
    board,plan=model(tmp_path);node=plan['fpgas'][0];ir,_=partition(tmp_path,node)
    out=tmp_path/'native'
    with pytest.raises(ValidationError,match='DUT clock'):
        write_vivado_word_top(ir,out,fpga=node['fpga'],board=board,plan=plan,dut_clock_ports=clock_ports)
    assert not out.exists()


def test_real_indexed_word_fixture_preparation_uses_current_native_owner(tmp_path,monkeypatch):
    from tests.test_native_schedule_generation import install_runtime
    from emuflow.io import read_json
    install_runtime(tmp_path,monkeypatch)
    path=Path(__file__).resolve().parents[1]/'scripts/validation/qualify_vivado_word_physical.py'
    spec=importlib.util.spec_from_file_location('word_physical_qualification',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    inputs=module.prepare(tmp_path)
    assert inputs['original_cells']==3 and inputs['transport_cells']==0
    assert inputs['mapped_report']['emitted_instances']==3
    assert len(inputs['local_query_path'].read_text().splitlines())==5
    report=write_vivado_word_top(inputs['ir_path'],tmp_path/'native',fpga=inputs['fpga'],
        board=read_json(inputs['word_wiring_path']),plan=read_json(inputs['word_phy_plan_path']))
    assert report['active_banks']==2
