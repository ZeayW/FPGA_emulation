`timescale 1ns/1ps
module ulx3s_endpoint_tb #(parameter real RX_HALF_PERIOD=20.2);
    reg aclk=0,bclk=0,reset=1;
    always #20 aclk=~aclk;
    initial begin #7; forever #(RX_HALF_PERIOD) bclk=~bclk; end
    reg [63:0] payload=0;
    reg send_valid=0;
    wire send_ready, a_wire, b_wire, got_valid, afault, bfault;
    wire [63:0] got;
    emuflow_gpio_endpoint a(.clk(aclk), .reset(reset), .serial_rx(b_wire), .serial_tx(a_wire),
        .tx_record(payload), .tx_valid(send_valid), .tx_ready(send_ready),
        .rx_record(got), .rx_valid(got_valid), .rx_ready(1'b1), .fault(afault));
    wire [63:0] echo;
    wire echo_valid,echo_ready;
    emuflow_gpio_endpoint b(.clk(bclk), .reset(reset), .serial_rx(a_wire), .serial_tx(b_wire),
        .tx_record(echo), .tx_valid(echo_valid), .tx_ready(echo_ready),
        .rx_record(echo), .rx_valid(echo_valid), .rx_ready(echo_ready), .fault(bfault));
    integer count=0, i;
    always @(posedge aclk) if(!reset) begin
        if(afault || bfault) $fatal(1,"endpoint fault");
        if(got_valid) begin
            if(got !== (64'h0123456789abcdef ^ count)) $fatal(1,"roundtrip mismatch");
            count=count+1;
        end
    end
    initial begin
        #300; @(negedge aclk); reset=0;
        for(i=0;i<16;i=i+1) begin
            @(negedge aclk); while(!send_ready) @(negedge aclk);
            payload=64'h0123456789abcdef ^ i; send_valid=1;
            @(negedge aclk); send_valid=0;
            wait(count==i+1);
        end
        $display("PASS 16 CRC-protected record roundtrips across independent clocks");
        $finish;
    end
    initial begin #10000000; $fatal(1,"timeout"); end
endmodule
