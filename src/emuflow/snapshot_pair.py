"""Compose mapped DUT partitions and checked transport with explicit host I/O.

The host interface is synchronous to board0, not yet a physical pin mapping.
No clock-domain crossing or board-level timing claim is made by this emitter.
"""
import re
from .errors import ValidationError
from .snapshot_netlist import emit_snapshot_partition
from .snapshot_rounds import derive_snapshot_rounds
from .board_ulx3s import ulx3s_pair_profile
from .ir import EmuIR


def bind_snapshot_reset_inputs(ir, ports):
    """Explicit synchronous reset-data view; never rewrite source semantics.

    Follow each requested input through combinational cells. Only ordinary
    data/synchronous-control endpoints are legal; clocks and asynchronous or
    unknown stateful primitives are rejected. No polarity or initial state is
    inferred. Board reset remains a separate service.
    """
    names=set(ports)
    if not names:
        return ir
    source_ports={p["id"]:p for p in ir.value["ports"]}
    if any(p not in source_ports or source_ports[p]["direction"]!="input" for p in names):
        raise ValidationError("reset-data binding requires named DUT input ports")
    cells={c["id"]:c for c in ir.value["instances"]}
    by_driver={}
    roots=[]
    for net in ir.value["nets"]:
        for ep in net["drivers"]:
            by_driver.setdefault(ep["instance"],[]).append(net)
        if any(ep["instance"] is None and ep["port"] in names for ep in net["drivers"]):
            if net["cut_class"]!="reset" or len(net["drivers"])!=1:
                raise ValidationError("reset-data input must identify a single-driver reset net")
            roots.append(net)
    found={n["drivers"][0]["port"] for n in roots}
    if found!=names:
        raise ValidationError("reset-data binding has no matching connected reset net")
    pending=list(roots); seen=set()
    safe={"$_DFF_P_":{"D"},"FDRE":{"D","CE","R"},"FDSE":{"D","CE","S"}}
    while pending:
        net=pending.pop()
        if net["id"] in seen: continue
        seen.add(net["id"])
        for ep in net["sinks"]:
            if ep["instance"] is None: continue
            cell=cells[ep["instance"]]; kind=cell["type"]
            if kind.startswith("LUT") or kind in {"$lut","$_LUT_"}:
                pending.extend(by_driver.get(ep["instance"],[]))
            elif ep["port"] not in safe.get(kind,set()):
                raise ValidationError("reset-data reaches a clock, asynchronous or unsupported endpoint")
    root_ids={n["id"] for n in roots}
    value=dict(ir.value)
    value["nets"]=[dict(n,cut_class="primary_input") if n["id"] in root_ids else n
                   for n in ir.value["nets"]]
    return EmuIR(value)


def emit_snapshot_pair(ir, assignment, *, prefix, port_owners, initial_state,
                       session_id, reset_data_ports=(), include_source_binding=False):
    """Emit both cores. Host data ports must explicitly belong to board0.

    A request samples the input vector once, then performs the derived number
    of exchanges. The response contains output values immediately before the
    corresponding original-DUT active edge, held until host acknowledgement.
    Neither response backpressure nor changed live host input advances state.
    """
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prefix):
        raise ValidationError("snapshot pair prefix must be a simple identifier")
    if type(session_id) is not int or not 0 <= session_id < 2**32:
        raise ValidationError("snapshot pair requires an explicit uint32 session")
    if any(owner != "board0" for owner in port_owners.values()):
        raise ValidationError("the host adapter owns data ports on board0 only")
    ir=bind_snapshot_reset_inputs(ir,reset_data_ports)
    rounds = derive_snapshot_rounds(ir, assignment, port_owners=port_owners)
    boards = {}
    for board in ("board0", "board1"):
        dut = f"{prefix}_{board}_dut"
        rtl, interface = emit_snapshot_partition(ir, assignment, board=board,
            module=dut, port_owners=port_owners, initial_state=initial_state,
            include_source_binding=include_source_binding)
        ni, no = max(1, len(interface["host_inputs"])), max(1, len(interface["host_outputs"]))
        ne, nr = max(1, len(interface["exported_nets"])), max(1, len(interface["imported_nets"]))
        width = interface["words"] * 32
        padding = width-ne
        payload = "exports" if not padding else f"{{{padding}'b0,exports}}"
        leader = int(board == "board0")
        core = f"{prefix}_{board}"
        rtl += f'''
module {core}(input wire clk,reset,link_rx,output wire link_tx,
    input wire [{ni-1}:0] host_inputs,input wire request_valid,
    output wire request_ready,output reg [{no-1}:0] host_outputs,
    output wire response_valid,input wire response_ready,output wire fault);
    reg [{ni-1}:0] held_inputs;
    reg pending,busy,response_pending;
    wire [{no-1}:0] dut_outputs;
    wire [{ne-1}:0] exports;
    wire [{width-1}:0] imports;
    wire commit,exchange_ready,protocol_fault,link_fault;
    wire [63:0] tx,rx;
    wire tv,tr,rv,rr;
    assign fault=protocol_fault || link_fault;
    assign request_ready={leader} && exchange_ready && !busy && !pending &&
                         !response_pending && !fault && !reset;
    assign response_valid=response_pending && !fault && !reset;
    {dut} dut(.clk(clk),.reset(reset),.step(commit && !fault && !reset),
        .host_inputs(held_inputs),.host_outputs(dut_outputs),
        .exported_values(exports),.imported_values(imports[{nr-1}:0]));
    always @(posedge clk) begin
        if(reset) begin
            held_inputs<=0; pending<=0; busy<=0;
            response_pending<=0; host_outputs<=0;
        end else if(!fault) begin
            if(response_valid && response_ready) response_pending<=0;
            if(request_valid && request_ready) begin
                held_inputs<=host_inputs; pending<=1; busy<=1;
            end
            // Delay launch by one edge: the accepted host input must precede
            // snapshot capture. This is a logical ordering, not a delay bound.
            if(pending && exchange_ready) pending<=0;
            if(commit && {leader}) begin
                host_outputs<=dut_outputs; response_pending<=1; busy<=0;
            end
        end
    end
    emuflow_gpio_endpoint endpoint(.clk(clk),.reset(reset),
        .serial_rx(link_rx),.serial_tx(link_tx),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr),.fault(link_fault));
    emuflow_gpio_exchange #(.LEADER({leader}),.WORDS({interface['words']}),.ROUNDS({rounds})) exchange(
        .clk(clk),.reset(reset),.session_id(32'h{session_id:08x}),
        .start(pending),.start_ready(exchange_ready),.local_snapshot({payload}),
        .remote_snapshot(imports),.commit(commit),.session_ready(),.fault(protocol_fault),
        .link_fault(link_fault),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr));
endmodule
'''
        physical_top = core+"_physical"
        host_ports = ",input wire host_rx,output wire host_tx" if leader else ""
        rtl += f'''
module {physical_top}(input wire clk_25mhz,reset_n,link_rx,output wire link_tx{host_ports});
    reg [1:0] reset_pipe=2'b11;
    always @(posedge clk_25mhz or negedge reset_n)
        if(!reset_n) reset_pipe<=2'b11;
        else reset_pipe<={{reset_pipe[0],1'b0}};
    wire reset=reset_pipe[1];
    wire [{ni-1}:0] inputs;
    wire [{no-1}:0] outputs;
    wire request_valid,request_ready,response_valid,response_ready,core_fault;
    {core} core(.clk(clk_25mhz),.reset(reset),.link_rx(link_rx),.link_tx(link_tx),
        .host_inputs(inputs),.request_valid(request_valid),.request_ready(request_ready),
        .host_outputs(outputs),.response_valid(response_valid),
        .response_ready(response_ready),.fault(core_fault));
'''
        if leader:
            divider = ulx3s_pair_profile()["host_interface"]["clocks_per_bit"]
            rtl += f'''
    wire [63:0] tx,rx;
    wire tv,tr,rv,rr,host_link_fault,host_fault;
    emuflow_gpio_endpoint #(.CLOCKS_PER_BIT({divider})) host_endpoint(
        .clk(clk_25mhz),.reset(reset),.serial_rx(host_rx),.serial_tx(host_tx),
        .tx_record(tx),.tx_valid(tv),.tx_ready(tr),.rx_record(rx),
        .rx_valid(rv),.rx_ready(rr),.fault(host_link_fault));
    emuflow_snapshot_host #(.INPUT_BITS({ni}),.OUTPUT_BITS({no}),.SESSION_ID(32'h{session_id:08x})) host(
        .clk(clk_25mhz),.reset(reset),.link_fault(host_link_fault),.core_fault(core_fault),
        .tx_record(tx),.tx_valid(tv),.tx_ready(tr),.rx_record(rx),.rx_valid(rv),.rx_ready(rr),
        .host_inputs(inputs),.request_valid(request_valid),.request_ready(request_ready),
        .host_outputs(outputs),.response_valid(response_valid),
        .response_ready(response_ready),.fault(host_fault));
'''
        else:
            rtl += "assign inputs=0; assign request_valid=0; assign response_ready=0;\n"
        rtl += "endmodule\n"
        boards[board] = {"rtl": rtl, "top": core, "physical_top": physical_top,
                         "host_uart": bool(leader), "interface": interface}
        if include_source_binding:
            # Exact wires connected above, relative to physical_top. These
            # are generated connectivity, not inferred post-synthesis names.
            boards[board]["physical_port_bindings"]={
                "imported_values":"core.imports", "exported_values":"core.exports",
                "host_inputs":"core.held_inputs", "host_outputs":"core.dut_outputs"}
    return {"boards": boards, "evaluation_rounds": rounds,
            "host_clock_domain": "board0", "output_sampling": "pre_dut_active_edge",
            "physical_host_binding_qualified": False}
