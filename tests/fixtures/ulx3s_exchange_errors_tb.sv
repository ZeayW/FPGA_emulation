`timescale 1ns/1ps
module ulx3s_exchange_errors_tb;
    reg clk=0,reset=1,start=0,lv=0,rxv=0;
    reg [31:0] session=32'h1234;
    reg [63:0] rx=0;
    wire [63:0] tx,remote;
    wire tv,rr,ready,joined,fault,commit;
    always #5 clk=~clk;
    emuflow_gpio_exchange #(.TIMEOUT_CYCLES(40)) dut(
        .clk(clk),.reset(reset),.session_id(session),.start(start),.start_ready(ready),
        .local_snapshot(64'h9876),.remote_snapshot(remote),.commit(commit),
        .session_ready(joined),.fault(fault),.link_fault(lv),
        .tx_record(tx),.tx_valid(tv),.tx_ready(1'b1),
        .rx_record(rx),.rx_valid(rxv),.rx_ready(rr));
    task restart;
        begin
            @(negedge clk); reset=1; start=0; rxv=0; lv=0; session=32'h1234;
            repeat(3) @(negedge clk); reset=0;
            wait(tv); @(negedge clk);
        end
    endtask
    task receive(input [63:0] record);
        begin
            @(negedge clk); while(!rr) @(negedge clk);
            rx=record; rxv=1; @(negedge clk); rxv=0;
        end
    endtask
    task failed;
        begin
            repeat(2) @(negedge clk);
            if(!fault || commit || ready || joined) $fatal(1,"not fail-stop");
        end
    endtask
    task connect;
        begin restart; receive(64'h1000000200001234); receive(64'h1100000100000000); wait(ready); end
    endtask
    initial begin
        restart; receive(64'h1000000200005678); failed; // wrong session
        restart; receive(64'h1001000200001234); failed; // same role
        restart; receive(64'h1000000100001234); failed; // wrong width
        restart; receive(64'h1000000200001234); receive(64'h1100000200000000); failed; // round mismatch
        restart; wait(fault); failed; // missing peer timeout
        connect; receive(64'h1000000200001234); failed; // independent peer reset
        connect; @(negedge clk); session=32'h5678; failed;
        connect; @(negedge clk); start=1; @(negedge clk); start=0;
        receive(64'h3001000000000001); failed; // out of order word index
        connect; @(negedge clk); start=1; @(negedge clk); start=0;
        receive(64'h3000000100000001); failed; // wrong epoch
        connect; @(negedge clk); start=1; @(negedge clk); start=0;
        receive(64'h3000000000000001); receive(64'h3001000000000002);
        wait(tv && tx[63:56]==8'h40); @(negedge clk);
        wait(rr); @(negedge clk); lv=1; rx=64'h5000000000000000; rxv=1;
        failed; // ACK with transport error must not commit
        $display("PASS session/role/width/reset/epoch/order/timeout/fault rejection");
        $finish;
    end
    initial begin #100000; $fatal(1,"test timeout"); end
endmodule
