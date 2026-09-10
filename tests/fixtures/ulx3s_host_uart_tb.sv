`timescale 1ns/1ps
// Serial host -> physical wrapper -> two-board DUT -> serial host.
// Behavioral UART/clock test, not measured FTDI hardware performance.
module ulx3s_host_uart_tb;
    reg ac=0,bc=0,hc=0,reset_n=0,reset=1;
    always #20 ac=~ac;
    initial begin #7; forever #20.2 bc=~bc; end
    initial begin #3; forever #19.8 hc=~hc; end
    wire aw,bw,htx,hrx;
    wire [63:0] rx;
    reg [63:0] tx=0;
    reg tv=0,rr=0;
    wire tr,rv,hfault;
    host_pair_board0_physical a(.clk_25mhz(ac),.reset_n(reset_n),.link_rx(bw),.link_tx(aw),
        .host_rx(htx),.host_tx(hrx));
    host_pair_board1_physical b(.clk_25mhz(bc),.reset_n(reset_n),.link_rx(aw),.link_tx(bw));
    emuflow_gpio_endpoint #(.CLOCKS_PER_BIT(217)) client(.clk(hc),.reset(reset),
        .serial_rx(hrx),.serial_tx(htx),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr),.fault(hfault));
    task send(input [63:0] value);
        begin
            @(negedge hc); while(!tr) @(negedge hc);
            tx=value; tv=1; @(negedge hc); tv=0;
        end
    endtask
    task receive(input [63:0] value);
        begin
            @(negedge hc); while(!rv) @(negedge hc);
            if(rx!==value || hfault) $fatal(1,"host serial response mismatch");
            rr=1; @(negedge hc); rr=0;
        end
    endtask
    integer i;
    reg reference=0,data;
    initial begin
        #300; reset_n=1; reset=0;
        send(64'h6001000000000789);
        receive(64'h6101000000010001);
        for(i=0;i<8;i=i+1) begin
            data=(i%3==0);
            send({8'h70,8'd0,16'(i),31'b0,data});
            send({8'h71,8'd0,16'(i),32'b0});
            receive({8'h72,8'd0,16'(i),31'b0,reference});
            receive({8'h73,8'd0,16'(i),32'b0});
            reference=reference^data;
        end
        if(a.core_fault || b.core_fault || a.host_fault) $fatal(1,"physical wrapper fault");
        $display("PASS host UART and generated physical wrappers: eight reference macrocycles");
        $finish;
    end
    initial begin #100000000; $fatal(1,"host serial timeout"); end
endmodule
