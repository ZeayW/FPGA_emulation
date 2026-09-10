`timescale 1ns/1ps
// Protocol/PHY test, not a real-RTL benchmark or global timing qualification.
module ulx3s_exchange_tb #(parameter real RX_HALF_PERIOD=20.2);
    reg aclk=0,bclk=0,reset=1,disconnected=0;
    always #20 aclk=~aclk;
    initial begin #7; forever #(RX_HALF_PERIOD) bclk=~bclk; end
    wire aw,bw,ac,bc,af,bf,alf,blf,ar,br,ready;
    wire [63:0] at,bt,ax,bx,aremote,bremote;
    wire atv,btv,atr,btr,axv,bxv,axr,bxr;
    reg start=0;
    reg [63:0] adata=64'h100, bdata=64'h900;
    integer an=0,bn=0,i;
    emuflow_gpio_endpoint aphy(.clk(aclk),.reset(reset),.serial_rx(disconnected?1'b1:bw),
        .serial_tx(aw),.tx_record(at),.tx_valid(atv),.tx_ready(atr),
        .rx_record(ax),.rx_valid(axv),.rx_ready(axr),.fault(alf));
    emuflow_gpio_endpoint bphy(.clk(bclk),.reset(reset),.serial_rx(disconnected?1'b1:aw),
        .serial_tx(bw),.tx_record(bt),.tx_valid(btv),.tx_ready(btr),
        .rx_record(bx),.rx_valid(bxv),.rx_ready(bxr),.fault(blf));
    emuflow_gpio_exchange #(.LEADER(1),.TIMEOUT_CYCLES(20000)) a(
        .clk(aclk),.reset(reset),.session_id(32'h1234),.start(start),.start_ready(ready),
        .local_snapshot(adata),.remote_snapshot(aremote),.commit(ac),.session_ready(ar),
        .fault(af),.link_fault(alf),.tx_record(at),.tx_valid(atv),.tx_ready(atr),
        .rx_record(ax),.rx_valid(axv),.rx_ready(axr));
    emuflow_gpio_exchange #(.LEADER(0),.TIMEOUT_CYCLES(20000)) b(
        .clk(bclk),.reset(reset),.session_id(32'h1234),.start(1'b0),.start_ready(),
        .local_snapshot(bdata),.remote_snapshot(bremote),.commit(bc),.session_ready(br),
        .fault(bf),.link_fault(blf),.tx_record(bt),.tx_valid(btv),.tx_ready(btr),
        .rx_record(bx),.rx_valid(bxv),.rx_ready(bxr));
    always @(posedge aclk) if(!reset && ac) begin
        if(af || alf || aremote !== 64'h900+an) $fatal(1,"leader snapshot/commit");
        adata <= adata+1; an=an+1;
    end
    always @(posedge bclk) if(!reset && bc) begin
        if(bf || blf || bremote !== 64'h100+bn) $fatal(1,"follower snapshot/commit");
        bdata <= bdata+1; bn=bn+1;
    end
    initial begin
        #300; @(negedge aclk); reset=0;
        wait(ar && br);
        for(i=0;i<8;i=i+1) begin
            @(negedge aclk); while(!ready) @(negedge aclk);
            start=1; @(negedge aclk); start=0;
            wait(an==i+1 && bn==i+1);
        end
        @(negedge aclk); while(!ready) @(negedge aclk);
        disconnected=1; start=1; @(negedge aclk); start=0;
        wait(af); repeat(10) @(negedge aclk);
        if(an!=8 || bn!=8 || ready) $fatal(1,"disconnected transaction committed");
        $display("PASS independent-clock snapshot exchange, logical commit, disconnect timeout");
        $finish;
    end
    initial begin #15000000; $fatal(1,"test timeout"); end
endmodule
