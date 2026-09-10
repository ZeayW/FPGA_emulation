// Physical endpoint qualification only, not a partitioned DUT benchmark.
// Echo received checked records; a protocol fault stops further communication.
module ulx3s_endpoint_physical (
    input wire clk_25mhz, reset_n, link_rx,
    output wire link_tx
);
    reg [1:0] reset_pipe;
    always @(posedge clk_25mhz or negedge reset_n)
        if(!reset_n) reset_pipe<=0;
        else reset_pipe<={reset_pipe[0],1'b1};
    wire [63:0] record;
    wire valid, ready, fault;
    emuflow_gpio_endpoint endpoint (
        .clk(clk_25mhz), .reset(!reset_pipe[1]), .serial_rx(link_rx), .serial_tx(link_tx),
        .tx_record(record), .tx_valid(valid), .tx_ready(ready),
        .rx_record(record), .rx_valid(valid), .rx_ready(ready), .fault(fault));
endmodule
