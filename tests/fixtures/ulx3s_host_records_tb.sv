`timescale 1ns/1ps
module ulx3s_host_records_tb;
    reg clk=0,reset=1,lf=0,cf=0,rv=0,tr=0,ready=0,valid=0;
    reg [63:0] rx=0;
    wire [63:0] tx;
    wire rr,tv,req,resp,fault;
    wire [32:0] inputs;
    always #5 clk=~clk;
    emuflow_snapshot_host #(.INPUT_BITS(33),.OUTPUT_BITS(33),.SESSION_ID(32'h789)) dut(
        .clk(clk),.reset(reset),.link_fault(lf),.core_fault(cf),
        .rx_record(rx),.rx_valid(rv),.rx_ready(rr),.tx_record(tx),.tx_valid(tv),.tx_ready(tr),
        .host_inputs(inputs),.request_valid(req),.request_ready(ready),
        .host_outputs(33'h1deadbeef),.response_valid(valid),.response_ready(resp),.fault(fault));
    task restart;
        begin
            @(negedge clk); reset=1; lf=0; cf=0; rv=0; tr=0; ready=0; valid=0;
            repeat(3) @(negedge clk); reset=0;
        end
    endtask
    task send(input [63:0] value);
        begin
            @(negedge clk); while(!rr) @(negedge clk);
            rx=value; rv=1; @(negedge clk); rv=0;
        end
    endtask
    task receive(input [63:0] expected);
        begin
            @(negedge clk); while(!tv) @(negedge clk);
            repeat(3) begin
                if(tx!==expected || !tv) $fatal(1,"host reply not stable/correct");
                @(negedge clk);
            end
            tr=1; @(negedge clk); tr=0;
        end
    endtask
    task connect;
        begin restart; send(64'h6001000000000789); receive(64'h6101000000210021); end
    endtask
    task failed;
        begin
            repeat(2) @(negedge clk);
            if(!fault || req || resp || rr || tv) $fatal(1,"host fault not fail-stop");
        end
    endtask
    integer n;
    initial begin
        connect;
        for(n=0;n<2;n=n+1) begin
            send({8'h70,8'd0,16'(n),32'h12345678});
            if(req) $fatal(1,"incomplete input executed");
            send({8'h70,8'd1,16'(n),32'd1});
            if(req) $fatal(1,"input executed without explicit command");
            send({8'h71,8'd0,16'(n),32'd0});
            wait(req); repeat(3) @(negedge clk);
            if(!req || inputs!==33'h112345678) $fatal(1,"request not retained");
            ready=1; @(negedge clk); ready=0;
            wait(resp); valid=1; @(negedge clk); valid=0;
            receive({8'h72,8'd0,16'(n),32'hdeadbeef});
            receive({8'h72,8'd1,16'(n),32'd1});
            receive({8'h73,8'd0,16'(n),32'd0});
        end
        connect; send(64'h7001000000000000); failed; // wrong first word
        connect; send(64'h7000000000000000); send(64'h7001000000000002); failed; // nonzero padding
        connect; send(64'h7100000000000000); failed; // execute before input
        restart; send(64'h6001000000000788); failed; // wrong session
        connect; @(negedge clk); cf=1; failed;
        $display("PASS host session, words, padding, execute, backpressure and fault protocol");
        $finish;
    end
    initial begin #100000; $fatal(1,"host protocol timeout"); end
endmodule
