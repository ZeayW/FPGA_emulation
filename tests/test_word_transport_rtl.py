"""Periodic word RTL qualification; the channel here is an ideal word model.

This checks actual emitted RTL, not vendor PHY latency or board signoff.
"""
import copy
import io
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.netlist import transport_to_systemverilog
from emuflow.runtime import virtual_runtime_controller_to_systemverilog
from emuflow.tdm_word import WORD_EVENT_POLICY, LVDSWordTiming
from emuflow.transport_storage import write_transport_storage, write_transport_systemverilog
from tests.test_tdm_word import board
from emuflow.platform import Platform


def fixture():
    platform = Platform.from_dict(board())
    link = replace(platform.links[0], data_lanes_per_direction=2, bank_count=None, wires_per_bank=None,
                   cable_count=None,hp_banks_per_cable=None)
    platform = replace(platform,links=(link,),virtual_board_model=None)
    documents = {}
    for fpga,peer,kind in (('fpga0','fpga1','tx'),('fpga1','fpga0','rx')):
        endpoints=[]
        for i in range(40):
            phase=(i//2)//8
            load=phase or 3
            endpoints.append(dict(id=f'{kind}{i}',kind=kind,fpga=fpga,peer=peer,link=link.id,
                signal=f'net:{i}'if kind=='tx'else f'shadow:{i}',lane=(i%2)*8+(i//2)%8,
                slot=load,arrival_slot=load+6,tdm_ratio=24,word_phase=phase))
        documents[fpga]=dict(fpga=fpga,frame_slots=23,word_event_policy=WORD_EVENT_POLICY,virtual_clock_order=['clk'],
            endpoints=endpoints,source_signals=[dict(index=i,signal=f'net:{i}')for i in range(40)]if kind=='tx'else[],
            shadow_signals=[dict(index=i,signal=f'shadow:{i}')for i in range(40)]if kind=='rx'else[])
    return platform,documents


def test_streamed_and_materialized_word_emitters_have_one_implementation(tmp_path):
    platform,documents=fixture()
    from emuflow.word_transport_rtl import materialized_rtl_rows
    for fpga,document in documents.items():
        groups,rows=materialized_rtl_rows(document)
        class Indexed(dict):
            def endpoint_groups(self):return groups
            def iter_rtl_endpoints(self,kind):return rows(kind)
            def __getitem__(self,key):
                if key=='endpoints':raise AssertionError('whole endpoint population materialized')
                return super().__getitem__(key)
        out=tmp_path/(fpga+'.sv')
        write_transport_systemverilog(out,Indexed(document),platform)
        assert out.read_text()==transport_to_systemverilog(document,platform)
        assert 'case (slot)'not in out.read_text()
        assert 'DONT_TOUCH'not in out.read_text()


@pytest.mark.parametrize('field,value',[('word_phase',True),('word_phase',3),('tdm_ratio',9),
                                         ('arrival_slot',8.0),('slot',0)])
def test_malformed_word_endpoint_is_rejected(field,value):
    platform,documents=fixture()
    document=copy.deepcopy(documents['fpga0']);document['endpoints'][0][field]=value
    with pytest.raises(ValidationError):transport_to_systemverilog(document,platform)


@pytest.mark.skipif(not shutil.which('iverilog') or not shutil.which('vvp'),reason='Icarus Verilog required')
def test_real_rtl_repeats_words_drains_at_barrier_and_realigns_the_next_epoch(tmp_path):
    platform,documents=fixture()
    for fpga,document in documents.items():
        (tmp_path/(fpga+'.sv')).write_text(transport_to_systemverilog(document,platform))
    runtime=dict(virtual_dut_clocks={'clk':{}},virtual_timebase={'events':[{'clocks':['clk']}]})
    (tmp_path/'controller.sv').write_text(virtual_runtime_controller_to_systemverilog(runtime))
    link=platform.links[0].id.replace('-','_')
    bench=r'''
`timescale 1ns/1ps
module tb;
reg clk=0; always #2.5 clk=~clk;
reg reset=1,ready=0;
reg [39:0] source_values=0;
wire [15:0] tx;
wire [39:0] shadows;
wire en0,en1;
wire [4:0] slot0,slot1;
reg [15:0] pipe[0:5];integer j;
always @(posedge clk) begin
 if(reset)for(j=0;j<6;j=j+1)pipe[j]<=0;
 else begin pipe[0]<=tx;for(j=1;j<6;j=j+1)pipe[j]<=pipe[j-1];end
end
emuflow_transport_fpga0 a(.fabric_clk(clk),.reset(reset),.links_ready(ready),.source_values(source_values),
 .shadow_values(),.virtual_clock_enable(en0),.slot_debug(slot0),.TX(tx));
emuflow_transport_fpga1 b(.fabric_clk(clk),.reset(reset),.links_ready(ready),.source_values(1'b0),
 .shadow_values(shadows),.virtual_clock_enable(en1),.slot_debug(slot1),.RX(pipe[5]));
integer i,w,phase,expected;
reg[39:0] captured=0;integer captures=0;
always @(posedge clk)if(en1)begin captured<=shadows;captures<=captures+1;end
initial begin
 repeat(2)@(negedge clk);reset=0;
 repeat(3)begin @(posedge clk);#0.01;if(tx!==0 ||shadows!==0 ||en0!==0)$fatal(1,"startup not gated");end
 @(negedge clk);ready=1;
 wait(a.transport_active);#0.01;
 for(w=1;w<=22;w=w+1)begin
  @(posedge clk);#0.01;
  for(i=0;i<40;i=i+1)begin
   phase=(i/2)/8;expected=phase==0?12:phase==1?13:14;
   if(shadows[i] !== (w>=expected))$fatal(1,"first epoch word mismatch w=%0d bit=%0d",w,i);
  end
  if(w==5)begin @(negedge clk);source_values='1;end
  if(w==20)begin @(negedge clk);ready=0;end
 end
 repeat(8)begin @(posedge clk);#0.01;
  if(shadows!==40'hffffffffff ||tx!==0||captures!=0||slot0!=22||slot1!=22)$fatal(1,"barrier drain/hold failed");
 end
 @(negedge clk);ready=1;source_values=0;
 @(posedge clk);#0.01;
 if(captures!=1||captured!==40'hffffffffff)$fatal(1,"DUT commit did not see final values");
 for(w=1;w<=22;w=w+1)begin
  @(posedge clk);#0.01;
  for(i=0;i<40;i=i+1)begin
   phase=(i/2)/8;expected=phase==0?9:phase==1?7:8;
   if(shadows[i] !== (w<expected))$fatal(1,"second epoch phase mismatch w=%0d bit=%0d",w,i);
  end
 end
 @(posedge clk);#0.01;
 if(captures!=2||captured!==0)$fatal(1,"second DUT commit failed");
 $display("WORD_TRANSPORT_PASS");$finish;
end
initial begin #2000;$fatal(1,"timeout");end
endmodule
'''.replace('.TX(',f'.tx_{link}_fpga1(').replace('.RX(',f'.rx_{link}_fpga0(')
    (tmp_path/'tb.sv').write_text(bench)
    exe=tmp_path/'sim'
    compile=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(exe),*(str(tmp_path/name)for name in ('controller.sv','fpga0.sv','fpga1.sv','tb.sv'))],capture_output=True,text=True)
    assert compile.returncode==0,compile.stderr
    result=subprocess.run(['vvp',str(exe)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'WORD_TRANSPORT_PASS'in result.stdout


def test_native_schedule_flows_through_indexed_phase6_without_endpoint_materialization(tmp_path,monkeypatch):
    from tests.native_schedule_case import _case
    from tests.test_native_schedule_generation import install_runtime
    from emuflow.tdm import build_native_exact_schedule_request
    from emuflow.tdm_schedule_storage import write_tdm_schedule_storage, read_tdm_schedule
    from emuflow.phase3_assignment_index import write_phase3_assignment_index
    from emuflow.netlist import build_split_artifacts
    case,routes=_case(tmp_path,"word",False)
    install_runtime(tmp_path,monkeypatch)
    schedule_path=tmp_path/'schedule.json'
    write_tdm_schedule_storage(schedule_path,build_native_exact_schedule_request(routes,case.platform))
    schedule=read_tdm_schedule(schedule_path)
    _clusters,assignment=case._exact_artifacts(dependent_return=True)
    assignment_path=tmp_path/'assignment.sqlite'
    write_phase3_assignment_index(assignment_path,case.ir,assignment)
    materialized=build_split_artifacts(case.ir,assignment,schedule.materialize(),case.platform)
    actual=write_transport_storage(tmp_path/'split',assignment_path,schedule,case.platform,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'])
    for fpga,view in actual['transports'].items():
        assert view['word_event_policy']==WORD_EVENT_POLICY
        assert all(endpoint['tdm_ratio']==8 and endpoint['word_phase']==0 for endpoint in view['endpoints'])
        out=tmp_path/(fpga+'.sv')
        write_transport_systemverilog(out,view,case.platform)
        assert out.read_text()==transport_to_systemverilog(materialized['transports'][fpga],case.platform)
        for anchor in actual['anchors'][fpga]['anchors']:
            assert anchor['physical_lane']==anchor['logical_lane']//8
            assert anchor['bit_within_physical_lane']==anchor['logical_lane']%8


@pytest.mark.skipif(not shutil.which('iverilog') or not shutil.which('vvp'),reason='Icarus Verilog required')
def test_relay_cannot_load_a_shadow_on_the_edge_that_updates_it(tmp_path):
    platform,documents=fixture()
    link=platform.links[0].id
    for fpga,peer in (('fpga0','fpga1'),('fpga1','fpga0')):
        doc=documents[fpga]
        doc['source_signals']=[dict(index=0,signal='net:launch')]if fpga=='fpga0'else[]
        doc['shadow_signals']=[dict(index=0,signal='shadow:rx')]
        doc['endpoints']=[]
        for kind in ('tx','rx'):
            # At fpga1, the outbound value is the newly received shadow.
            signal='net:launch'if fpga=='fpga0'and kind=='tx'else'shadow:rx'
            load=8 if fpga=='fpga1'and kind=='tx'or fpga=='fpga0'and kind=='rx'else 1
            doc['endpoints'].append(dict(id=fpga+kind,kind=kind,fpga=fpga,peer=peer,
                link=link,signal=signal,lane=0,slot=load,arrival_slot=load+6,
                tdm_ratio=8,word_phase=0))
        (tmp_path/(fpga+'.sv')).write_text(transport_to_systemverilog(doc,platform))
    runtime=dict(virtual_dut_clocks={'clk':{}},virtual_timebase={'events':[{'clocks':['clk']}]})
    (tmp_path/'controller.sv').write_text(virtual_runtime_controller_to_systemverilog(runtime))
    bench=r'''
`timescale 1ns/1ps
module tb;
reg clk=0;always #2.5 clk=~clk;
reg reset=1,ready=0;
wire[15:0] tx0,tx1;
wire[0:0] shadow0,shadow1;
reg[15:0] forward_pipe[0:5],return_pipe[0:5];integer j,w;
always @(posedge clk)begin
 if(reset)for(j=0;j<6;j=j+1)begin forward_pipe[j]<=0;return_pipe[j]<=0;end
 else begin
  forward_pipe[0]<=tx0;return_pipe[0]<=tx1;
  for(j=1;j<6;j=j+1)begin forward_pipe[j]<=forward_pipe[j-1];return_pipe[j]<=return_pipe[j-1];end
 end
end
emuflow_transport_fpga0 a(.fabric_clk(clk),.reset(reset),.links_ready(ready),
 .source_values(1'b1),.shadow_values(shadow0),.virtual_clock_enable(),.slot_debug(),
 .TX0(tx0),.RX0(return_pipe[5]));
emuflow_transport_fpga1 b(.fabric_clk(clk),.reset(reset),.links_ready(ready),
 .source_values(1'b0),.shadow_values(shadow1),.virtual_clock_enable(),.slot_debug(),
 .TX1(tx1),.RX1(forward_pipe[5]));
initial begin
 repeat(2)@(negedge clk);reset=0;ready=1;
 wait(a.transport_active);#0.01;
 for(w=1;w<=20;w=w+1)begin
  @(posedge clk);#0.01;
  if(shadow1[0] !== (w>=7))$fatal(1,"first hop wrong at word %0d",w);
  if(shadow0[0] !== (w>=14))$fatal(1,"relay sampled before its source shadow was ready at word %0d",w);
 end
 $display("WORD_RELAY_PASS");$finish;
end
initial begin #500;$fatal(1,"timeout");end
endmodule
'''
    link=link.replace('-','_')
    for marker,port in (('TX0',f'tx_{link}_fpga1'),('RX0',f'rx_{link}_fpga1'),
                        ('TX1',f'tx_{link}_fpga0'),('RX1',f'rx_{link}_fpga0')):
        bench=bench.replace('.'+marker+'(','.'+port+'(')
    (tmp_path/'tb.sv').write_text(bench)
    exe=tmp_path/'sim'
    compiled=subprocess.run(['iverilog','-g2012','-s','tb','-o',str(exe),
        *(str(tmp_path/name)for name in ('controller.sv','fpga0.sv','fpga1.sv','tb.sv'))],capture_output=True,text=True)
    assert compiled.returncode==0,compiled.stderr
    result=subprocess.run(['vvp',str(exe)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'WORD_RELAY_PASS'in result.stdout
