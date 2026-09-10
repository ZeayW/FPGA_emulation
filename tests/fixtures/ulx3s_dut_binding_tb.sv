`timescale 1ns/1ps
// Unequal-width interface qualification, never an application benchmark.
module binding_left(input wire clk,reset,step,
    input wire [16:0] imported_values, output reg [32:0] exported_values);
    always @(posedge clk)
        if(reset) exported_values<=33'h100000001;
        else if(step) exported_values<=33'h100000000+imported_values+1;
endmodule
module binding_right(input wire clk,reset,step,
    input wire [32:0] imported_values, output reg [16:0] exported_values);
    always @(posedge clk)
        if(reset) exported_values<=17'd9;
        else if(step) exported_values<=imported_values[16:0]+2;
endmodule
module ulx3s_dut_binding_tb;
    reg aclk=0,bclk=0,reset_n=0;
    always #20 aclk=~aclk;
    initial begin #7; forever #20.2 bclk=~bclk; end
    wire aw,bw;
    binding_board0 a(.clk_25mhz(aclk),.reset_n(reset_n),.link_rx(bw),.link_tx(aw));
    binding_board1 b(.clk_25mhz(bclk),.reset_n(reset_n),.link_rx(aw),.link_tx(bw));
    reg [32:0] expected_a[0:16];
    reg [16:0] expected_b[0:16];
    integer an=0,bn=0,i;
    always @(posedge aclk) if(!a.reset && a.dut.step) begin
        if(a.fault || a.remote_values[16:0]!==expected_b[an]) $fatal(1,"left import mismatch");
        an=an+1;
        #1; if(a.exported_values!==expected_a[an]) $fatal(1,"left macrocycle mismatch");
    end
    always @(posedge bclk) if(!b.reset && b.dut.step) begin
        if(b.fault || b.remote_values[32:0]!==expected_a[bn]) $fatal(1,"right import mismatch");
        bn=bn+1;
        #1; if(b.exported_values!==expected_b[bn]) $fatal(1,"right macrocycle mismatch");
    end
    initial begin
        expected_a[0]=33'h100000001; expected_b[0]=9;
        for(i=1;i<=16;i=i+1) begin
            expected_a[i]=33'h100000000+expected_b[i-1]+1;
            expected_b[i]=expected_a[i-1][16:0]+2;
        end
        #300; reset_n=1;
        wait(an==16 && bn==16);
        $display("PASS generated DUT bindings: 16 reference macrocycles, unequal widths, independent clocks");
        $finish;
    end
    initial begin #15000000; $fatal(1,"binding test timeout"); end
endmodule
