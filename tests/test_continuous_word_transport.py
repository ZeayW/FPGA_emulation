"""Exercise continuous physical payloads across virtual frame boundaries."""
import copy
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.runtime import virtual_runtime_controller_to_systemverilog
from emuflow.word_transport_rtl import (
    PHYSICAL_WORD_CALENDAR_POLICY,
    materialized_rtl_rows,
    write_physical_word_transport,
)
from tests.test_word_transport_rtl import fixture


def test_continuous_physical_lowering_streams_the_same_indexed_endpoint_owner(tmp_path):
    platform, documents = fixture()
    document = documents['fpga0']
    groups, rows = materialized_rtl_rows(document)

    class Indexed(dict):
        def endpoint_groups(self): return groups
        def iter_rtl_endpoints(self, kind): return rows(kind)
        def __getitem__(self, key):
            if key == 'endpoints': raise AssertionError('materialized endpoint population')
            return super().__getitem__(key)

    first, second = tmp_path/'first.sv', tmp_path/'second.sv'
    report = write_physical_word_transport(first, document, platform)
    write_physical_word_transport(second, Indexed(document), platform)
    assert first.read_bytes() == second.read_bytes()
    assert report == dict(policy=PHYSICAL_WORD_CALENDAR_POLICY,
                         rtl=dict(path=str(first.resolve()), sha256=file_sha256(first)))
    assert 'word_edge + 6 < FRAME_SLOTS' not in first.read_text()


def test_malformed_physical_endpoint_cannot_replace_previous_rtl(tmp_path):
    platform, documents = fixture()
    document = copy.deepcopy(documents['fpga0'])
    document['endpoints'][0]['word_phase'] = 3
    output = tmp_path/'transport.sv'
    output.write_text('previous transport source\n')
    with pytest.raises(ValidationError):
        write_physical_word_transport(output, document, platform)
    assert output.read_text() == 'previous transport source\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['transport.sv']


def test_completed_calendar_cannot_silently_relabel_historical_rtl(tmp_path):
    from emuflow.multi_fpga_physical_flow import _word_physical_calendar_policy
    assert _word_physical_calendar_policy({}) == 'legacy-drained-word-epoch-v1'
    platform, documents = fixture()
    calendar = write_physical_word_transport(tmp_path/'transport.sv', documents['fpga0'], platform)
    record = dict(stages=dict(transport_synthesis=dict(physical_calendar=calendar)))
    assert _word_physical_calendar_policy(record) == PHYSICAL_WORD_CALENDAR_POLICY
    Path(calendar['rtl']['path']).write_text('changed source')
    with pytest.raises(ValidationError, match='calendar RTL differs'):
        _word_physical_calendar_policy(record)
    record['stages']['transport_synthesis']['physical_calendar']['policy'] = 'unknown'
    with pytest.raises(ValidationError, match='unsupported physical calendar'):
        _word_physical_calendar_policy(record)


@pytest.mark.skipif(not shutil.which('iverilog') or not shutil.which('vvp'), reason='Icarus Verilog required')
@pytest.mark.parametrize('ratio', [8, 24, 32])
def test_physical_payload_never_drains_or_realigns_at_virtual_frames(tmp_path, ratio):
    platform, documents = fixture()
    words = ratio//8
    count = min(40, 16*words)
    for fpga, document in documents.items():
        document['endpoints'] = document['endpoints'][:count]
        field = 'source_signals' if fpga == 'fpga0' else 'shadow_signals'
        document[field] = document[field][:count]
        for endpoint in document['endpoints']:
            endpoint['tdm_ratio'] = ratio
            endpoint['slot'] = endpoint['word_phase'] or words
            endpoint['arrival_slot'] = endpoint['slot']+6
        write_physical_word_transport(tmp_path/(fpga+'.sv'), document, platform)
    runtime = dict(virtual_dut_clocks={'clk': {}}, virtual_timebase={'events': [{'clocks': ['clk']}]})
    (tmp_path/'controller.sv').write_text(virtual_runtime_controller_to_systemverilog(runtime))
    bench = r'''
`timescale 1ns/1ps
module tb;
localparam N=COUNT, PERIOD=WORDS;
reg clk=0; always #2.5 clk=~clk;
reg reset=1,ready=0;
reg [N-1:0] source_values=0;
wire [15:0] tx;
wire [N-1:0] shadows;
reg [15:0] pipe[0:5], sent[0:200];
reg [N-1:0] expected;
integer i,j,w,trial,lane,phase;
integer previous_load[0:N-1];
always @(posedge clk) begin
 if(reset) for(j=0;j<6;j=j+1) pipe[j]<=0;
 else begin pipe[0]<=tx;for(j=1;j<6;j=j+1)pipe[j]<=pipe[j-1];end
end
emuflow_transport_fpga0 a(.fabric_clk(clk),.reset(reset),.links_ready(ready),
 .source_values(source_values),.shadow_values(),.virtual_clock_enable(),.slot_debug(),.TX(tx));
emuflow_transport_fpga1 b(.fabric_clk(clk),.reset(reset),.links_ready(ready),
 .source_values(1'b0),.shadow_values(shadows),.virtual_clock_enable(),.slot_debug(),.RX(pipe[5]));
initial begin
 repeat(3)@(negedge clk);reset=0;
 repeat(2)begin @(posedge clk);#0.01;
  if(tx!==0 || shadows!==0)$fatal(1,"payload before readiness");
 end
 for(trial=0;trial<2;trial=trial+1)begin
  @(negedge clk);ready=1;
  wait(a.payload_active);#0.01;
  expected=0;
  for(i=0;i<N;i=i+1)previous_load[i]=0;
  // 200 words crosses eight 23-word virtual boundaries. Ratio 24 has a
  // three-word calendar that does not divide the virtual frame length.
  for(w=1;w<=200;w=w+1)begin
   @(negedge clk);
   for(i=0;i<N;i=i+1)source_values[i]=((w*13+i*7+trial*3)>>(i%5))&1;
   #0.01;
   for(i=0;i<N;i=i+1)begin
    lane=(i%2)*8+(i/2)%8;phase=(i/2)/8;
    if((w%PERIOD)==phase)begin
     if(tx[lane]!==source_values[i])$fatal(1,"TX calendar changed w=%0d bit=%0d",w,i);
     if(previous_load[i]!=0 && w-previous_load[i]!=PERIOD)$fatal(1,"TX gap exceeds ratio");
     previous_load[i]=w;
    end
   end
   @(posedge clk);sent[w]=tx;
   if(w>6)for(i=0;i<N;i=i+1)begin
    lane=(i%2)*8+(i/2)%8;phase=(i/2)/8;
    if(((w-6)%PERIOD)==phase)expected[i]=sent[w-6][lane];
   end
   #0.01;
   if(shadows!==expected)$fatal(1,"RX delivery differs w=%0d trial=%0d",w,trial);
  end
  @(negedge clk);ready=0;#0.01;
  if(tx!==0)$fatal(1,"TX did not stop on readiness loss");
  repeat(3)begin @(posedge clk);#0.01;
   if(shadows!==0 || a.payload_active!==0)$fatal(1,"payload not cleared on readiness loss");
  end
 end
 $display("CONTINUOUS_WORD_PASS");$finish;
end
initial begin #10000;$fatal(1,"timeout");end
endmodule
'''.replace('COUNT', str(count)).replace('WORDS', str(words))
    link = platform.links[0].id.replace('-', '_')
    bench = bench.replace('.TX(', f'.tx_{link}_fpga1(').replace('.RX(', f'.rx_{link}_fpga0(')
    (tmp_path/'tb.sv').write_text(bench)
    exe = tmp_path/'sim'
    result = subprocess.run(['iverilog', '-g2012', '-s', 'tb', '-o', str(exe),
                             *(str(tmp_path/n) for n in ('controller.sv', 'fpga0.sv', 'fpga1.sv', 'tb.sv'))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['vvp', str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'CONTINUOUS_WORD_PASS' in result.stdout
