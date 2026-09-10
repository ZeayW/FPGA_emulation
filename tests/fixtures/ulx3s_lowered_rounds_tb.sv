`timescale 1ns/1ps
// Generated EmuIR lowerer + board tops + UART/record/exchange composition.
// Closed semantic fixture, not a benchmark or physical timing qualification.
module ulx3s_lowered_rounds_tb;
    reg ac=0,bc=0,reset_n=0;
    always #20 ac=~ac;
    initial begin #7; forever #20.2 bc=~bc; end
    wire aw,bw;
    lowered_top_board0 a(.clk_25mhz(ac),.reset_n(reset_n),.link_rx(bw),.link_tx(aw));
    lowered_top_board1 b(.clk_25mhz(bc),.reset_n(reset_n),.link_rx(aw),.link_tx(bw));
    integer an=0,bn=0;
    // The original two registers swap values each macrocycle; two inversions
    // on A -> B -> A -> B do not change the final Boolean value.
    always @(posedge ac) if(!a.reset) begin
        if(a.fault || a.link_fault) $fatal(1,"leader fault");
        if(a.dut.step) begin
            an=an+1;
            #1; if(a.exported_values[1] !== (an%2==1)) $fatal(1,"lowered A stale state");
        end
    end
    always @(posedge bc) if(!b.reset) begin
        if(b.fault || b.link_fault) $fatal(1,"follower fault");
        if(b.dut.step) begin
            bn=bn+1;
            #1; if(b.exported_values[1] !== (bn%2==0)) $fatal(1,"lowered B stale state");
        end
    end
    initial begin
        #300; reset_n=1;
        wait(an==16 && bn==16); #2;
        $display("PASS generated partitions and three-round UART: 16 reference macrocycles");
        $finish;
    end
    initial begin #40000000; $fatal(1,"timeout"); end
endmodule
