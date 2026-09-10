"""Explicit RTL qualification of snapshot macrocycles against unsplit logic.

Not a production replay or a physical timing proof. The generated testbench
drives real UART/record/exchange RTL and observes every mapped FF after commit.
"""
from .equivalence import _MappedModel
from .errors import ValidationError


def build_snapshot_equivalence_testbench(ir, assignment, pair, *, initial_state,
                                         vectors, max_board_cycles=20000000, timing_events=False):
    if type(timing_events) is not bool:raise ValidationError('timing_events must be an explicit boolean')
    if type(max_board_cycles) is not int or max_board_cycles <= 0:
        raise ValidationError("RTL qualification requires a positive cycle timeout")
    vectors = list(vectors)
    if not vectors:
        raise ValidationError("RTL qualification requires explicit input vectors")
    model = _MappedModel(ir)
    if set(initial_state) != set(model.ff_ids) or any(type(v) is not int or v not in (0, 1) for v in initial_state.values()):
        raise ValidationError("RTL qualification requires exact binary FF initial state")
    boards = pair["boards"]
    interface = boards["board0"]["interface"]
    inputs, outputs = interface["host_inputs"], interface["host_outputs"]
    ni, no = max(1, len(inputs)), max(1, len(outputs))
    widths = {p["id"]: p["width"] for p in ir.value["ports"]
              if p["id"] in {bit["port"] for bit in inputs}}
    state_points = {}
    # These internal names are the lowerer's canonical FF names. A change to
    # that naming contract fails compilation rather than silently losing state
    # coverage. This qualification-only view is not persisted in runtime RTL.
    for board, instance in (("board0", "a"), ("board1", "b")):
        local = sorted(c["id"] for c in ir.value["instances"] if assignment[c["id"]] == board)
        for index, name in enumerate(local):
            if name in initial_state:
                state_points[name] = f"{instance}.dut.state{index}"
    if set(state_points) != set(model.ff_ids):
        raise ValidationError("RTL state observation coverage is incomplete")
    state = dict(initial_state)
    steps = []
    for cycle, vector in enumerate(vectors):
        if (set(vector) != set(widths) or any(type(vector[p]) is not int or not 0 <= vector[p] < 1 << w for p, w in widths.items())):
            raise ValidationError("each RTL vector must cover every connected host input with an in-range value")
        bit_values = {(p, bit): (vector[p] >> bit) & 1 for p, w in widths.items() for bit in range(w)}
        # Clock value is irrelevant to settled combinational evaluation; set it
        # explicitly so the reference never creates random implicit stimulus.
        for net in ir.value["nets"]:
            if net["cut_class"] == "clock":
                for ep in net["drivers"]:
                    if ep["instance"] is None: bit_values[(ep["port"], ep["bit"])] = 0
        if set(model.top_input_net) - set(bit_values):
            raise ValidationError("unbound reference-model input")
        _, next_state, expected = model.evaluate(state, cycle, 0, input_values=bit_values)
        packed_in = sum(bit_values[(v["port"], v["bit"])] << v["index"] for v in inputs)
        packed_out = sum(expected[f'{v["port"]}[{v["bit"]}]'] << v["index"] for v in outputs)
        steps += ["@(negedge ac); while(!ready) @(negedge ac);",
                  f"host_input={ni}'h{packed_in:x}; request=1;",
                  "@(negedge ac); request=0; host_input=~host_input;",
                  "while(!valid) @(negedge ac);",
                  f'if(value!=={no}\'h{packed_out:x}) $fatal(1,"output mismatch cycle {cycle}");']
        for name in sorted(state_points):
            steps.append(f'if({state_points[name]}!==1\'b{next_state[name]}) $fatal(1,"state mismatch cycle {cycle}");')
        steps += ["repeat(3) @(negedge ac);",
                  f'if(!valid || ready || value!=={no}\'h{packed_out:x}) $fatal(1,"response hold mismatch");',
                  "ack=1; @(negedge ac); ack=0;"]
        state = next_state
    event_monitors=''
    if timing_events:
        for board,instance,clock in [('board0','a','ac'),('board1','b','bc')]:
            e=instance+'.exchange'
            def record(kind,epoch=None,round_=None,word="0"):
                return (f'$display("SNAPSHOT_EVENT {board} %.3f {kind} %0d %0d %0d", '
                    f'$realtime, {epoch or e+".epoch"}, {round_ or e+".evaluation_round"}, {word});')
            # Observe the SAME pre-NBA guards as the source RTL. FINISH's
            # snapshot belongs to the next epoch/round assigned on this edge.
            event_monitors+=f'''
always @(negedge reset) begin {record('reset_release',"0","0")} end
always @(posedge {clock}) if(!reset && !{instance}.fault) begin
  if({instance}.request_valid && {instance}.request_ready) begin {record('host_latch')} end
  if({instance}.dut.step) begin {record('commit')} end
  if({e}.state=={e}.S_DATA && {e}.tx_valid && {e}.tx_ready) begin
    {record('tx_data',word=e+'.index')}
  end
  if(({e}.state=={e}.IDLE && {e}.LEADER && {e}.start && !{e}.rx_valid) ||
     ({e}.state=={e}.W_PREPARE && {e}.rx_valid && {e}.rx_ready &&
      {e}.rx_record=={{{e}.PREPARE,8'b0,{e}.epoch,32'b0}})) begin
    {record('tx_capture')}
  end
  if({e}.state=={e}.FINISH && {e}.epoch!=16'hffff && {e}.evaluation_round!={e}.ROUNDS-1) begin
    {record('tx_capture',e+'.epoch+1',e+'.evaluation_round+1')}
  end
  if({e}.state=={e}.W_DATA && {e}.rx_valid && {e}.rx_ready &&
     {e}.rx_record[63:32]=={{{e}.DATA,{e}.index,{e}.epoch}}) begin
    {record('rx_update',word=e+'.index')}
  end
end
'''
    text = f'''`timescale 1ns/1ps
module snapshot_equivalence_tb;
reg ac=0,bc=0,reset=1,request=0,ack=0;
reg [{ni-1}:0] host_input=0;
wire aw,bw,ready,valid,af,bf;
wire [{no-1}:0] value;
always #20 ac=~ac;
initial begin #7; forever #20.2 bc=~bc; end
{boards['board0']['top']} a(.clk(ac),.reset(reset),.link_rx(bw),.link_tx(aw),
 .host_inputs(host_input),.request_valid(request),.request_ready(ready),
 .host_outputs(value),.response_valid(valid),.response_ready(ack),.fault(af));
{boards['board1']['top']} b(.clk(bc),.reset(reset),.link_rx(aw),.link_tx(bw),
 .host_inputs(1'b0),.request_valid(1'b0),.request_ready(),.host_outputs(),
 .response_valid(),.response_ready(1'b0),.fault(bf));
always @(negedge ac) if(!reset && (af!==1'b0 || bf!==1'b0)) $fatal(1,"transport fault");
{event_monitors}
initial begin repeat({max_board_cycles}) @(posedge ac); $fatal(1,"macrocycle timeout"); end
initial begin
 #300; reset=0;
{chr(10).join(steps)}
 $display("PASS snapshot macrocycles={len(vectors)} observed_ff={len(state_points)}");
 $finish;
end
endmodule
'''
    return text, {"macrocycles": len(vectors), "observed_ff": len(state_points),
                  "observed_output_bits": len(outputs),
                  "timing_event_scope": "simulated_protocol_events_not_measured_link" if timing_events else None,
                  "scope": "declared-input-trace-and-initial-state", "physical_timing_proof": False}
