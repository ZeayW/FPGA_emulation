`timescale 1ns/1ps
// Three actual crossings: A register -> B inverter -> A inverter -> B register.
// This is a semantic fixture, not a real workload or physical timing benchmark.
module ulx3s_rounds_tb #(parameter integer ROUND_COUNT=3);
    reg ac=0,bc=0,reset=1;
    always #20 ac=~ac;
    initial begin #7; forever #20.2 bc=~bc; end
    wire aw,bw,acommit,bcommit,af,bf,alf,blf,ready;
    wire [63:0] at,bt,ar,br;
    wire atv,btv,atr,btr,arv,brv,arr,brr;
    wire [31:0] av,bv;
    reg q=0,observed=0;
    integer an=0,bn=0;
    emuflow_gpio_endpoint ap(.clk(ac),.reset(reset),.serial_rx(bw),.serial_tx(aw),
        .tx_record(at),.tx_valid(atv),.tx_ready(atr),.rx_record(ar),.rx_valid(arv),.rx_ready(arr),.fault(alf));
    emuflow_gpio_endpoint bp(.clk(bc),.reset(reset),.serial_rx(aw),.serial_tx(bw),
        .tx_record(bt),.tx_valid(btv),.tx_ready(btr),.rx_record(br),.rx_valid(brv),.rx_ready(brr),.fault(blf));
    emuflow_gpio_exchange #(.LEADER(1),.WORDS(1),.ROUNDS(ROUND_COUNT)) a(
        .clk(ac),.reset(reset),.session_id(32'h731),.start(ready),.start_ready(ready),
        .local_snapshot({30'b0,~av[0],q}),.remote_snapshot(av),.commit(acommit),.session_ready(),
        .fault(af),.link_fault(alf),.tx_record(at),.tx_valid(atv),.tx_ready(atr),
        .rx_record(ar),.rx_valid(arv),.rx_ready(arr));
    emuflow_gpio_exchange #(.LEADER(0),.WORDS(1),.ROUNDS(ROUND_COUNT)) b(
        .clk(bc),.reset(reset),.session_id(32'h731),.start(1'b0),.start_ready(),
        .local_snapshot({31'b0,~bv[0]}),.remote_snapshot(bv),.commit(bcommit),.session_ready(),
        .fault(bf),.link_fault(blf),.tx_record(bt),.tx_valid(btv),.tx_ready(btr),
        .rx_record(br),.rx_valid(brv),.rx_ready(brr));
    always @(posedge ac) if(!reset) begin
        if(af || alf) $fatal(1,"leader fault");
        if(acommit) begin q<=~q; an=an+1; end
    end
    always @(posedge bc) if(!reset) begin
        if(bf || blf) $fatal(1,"follower fault");
        if(bcommit) begin
            if(bv[1] !== (bn%2==1)) $fatal(1,"stale combinational chain value");
            observed<=bv[1]; bn=bn+1;
        end
    end
    initial begin
        #300; reset=0;
        wait(an==8 && bn==8); #2;
        $display("PASS three-crossing chain: eight macrocycles over independent-clock UART");
        $finish;
    end
    initial begin #20000000; $fatal(1,"timeout"); end
endmodule
