`timescale 1ns/1ps
module ulx3s_host_tb;
    reg ac=0,bc=0,reset=1,input_bit=0,req=0,ack=0;
    always #20 ac=~ac;
    initial begin #7; forever #20.2 bc=~bc; end
    wire aw,bw,ready,valid,value,af,bf;
    host_pair_board0 a(.clk(ac),.reset(reset),.link_rx(bw),.link_tx(aw),
        .host_inputs(input_bit),.request_valid(req),.request_ready(ready),
        .host_outputs(value),.response_valid(valid),.response_ready(ack),.fault(af));
    host_pair_board1 b(.clk(bc),.reset(reset),.link_rx(aw),.link_tx(bw),
        .host_inputs(1'b0),.request_valid(1'b0),.request_ready(),
        .host_outputs(),.response_valid(),.response_ready(1'b0),.fault(bf));
    reg reference=0,accepted;
    integer i;
    initial begin
        #300; reset=0;
        for(i=0;i<16;i=i+1) begin
            @(negedge ac); while(!ready) @(negedge ac);
            accepted=(i%3==0); input_bit=accepted; req=1;
            @(negedge ac); req=0; input_bit=~accepted;
            // In-flight changes must not change the latched macrocycle input.
            while(!valid) begin
                @(negedge ac); input_bit=~input_bit;
                if(af || bf) $fatal(1,"host transaction fault");
            end
            repeat(10) begin
                @(negedge ac);
                if(!valid || ready || value!==reference)
                    $fatal(1,"host response or backpressure mismatch");
            end
            reference=reference^accepted;
            ack=1; @(negedge ac); ack=0;
        end
        $display("PASS host input sampling, pre-edge output, response backpressure: 16 macrocycles");
        $finish;
    end
    initial begin #30000000; $fatal(1,"host test timeout"); end
endmodule
