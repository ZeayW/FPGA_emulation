`timescale 1ns/1ps
// Lowerer semantics only; UART and whole-flow acceptance are separate gates.
module snapshot_netlist_tb;
    reg clk=0,reset=1,step=0,ai=0,bi=0;
    always #5 clk=~clk;
    wire ao,bo,result;
    partition_board0 a(.clk(clk),.reset(reset),.step(step),.imported_values(ai),
        .exported_values(ao),.host_inputs(1'b0),.host_outputs(result));
    partition_board1 b(.clk(clk),.reset(reset),.step(step),.imported_values(bi),
        .exported_values(bo),.host_inputs(1'b0),.host_outputs());
    reg ref_a=0,ref_b=0;
    always @(posedge clk)
        if(reset) begin ref_a<=0; ref_b<=0; end
        else if(step) begin ref_a<=~ref_b; ref_b<=ref_a; end
    integer i;
    initial begin
        repeat(3) @(negedge clk); reset=0;
        for(i=0;i<32;i=i+1) begin
            @(negedge clk); ai=bo; bi=ao;
            repeat(3) begin
                @(negedge clk);
                if(result!==ref_a || bo!==~ref_b) $fatal(1,"state changed while paused");
            end
            step=1; @(negedge clk); step=0;
            if(result!==ref_a || bo!==~ref_b) $fatal(1,"lowered partition disagrees with reference");
        end
        $display("PASS EmuIR-lowered partitions: 32 macrocycles, combinational cut, pause enables");
        $finish;
    end
    initial begin #10000; $fatal(1,"timeout"); end
endmodule
