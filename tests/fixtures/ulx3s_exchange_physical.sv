// Physical qualification fixture only; not an application benchmark.
module ulx3s_exchange_physical # (parameter integer LEADER=1) (
    input wire clk_25mhz, reset_n, link_rx,
    output wire link_tx
);
    reg [1:0] reset_pipe=2'b11;
    always @(posedge clk_25mhz or negedge reset_n)
        if(!reset_n) reset_pipe<=2'b11;
        else reset_pipe<={reset_pipe[0],1'b0};
    wire reset=reset_pipe[1];
    wire [63:0] tx,rx,remote;
    wire tv,tr,rv,rr,phy_fault,fault,commit,ready;
    reg [63:0] state;
    always @(posedge clk_25mhz)
        if(reset) state<=64'h1;
        else if(commit && !fault) state <= (state+1)^remote;
    emuflow_gpio_endpoint endpoint(.clk(clk_25mhz),.reset(reset),
        .serial_rx(link_rx),.serial_tx(link_tx),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr),.fault(phy_fault));
    emuflow_gpio_exchange #(.LEADER(LEADER)) exchange(.clk(clk_25mhz),.reset(reset),
        .session_id(32'h1234),.start(ready),.start_ready(ready),.local_snapshot(state),
        .remote_snapshot(remote),.commit(commit),.session_ready(),.fault(fault),
        .link_fault(phy_fault),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr));
endmodule

module ulx3s_exchange_follower_physical (
    input wire clk_25mhz, reset_n, link_rx,
    output wire link_tx
);
    ulx3s_exchange_physical #(.LEADER(0)) core(.*);
endmodule
