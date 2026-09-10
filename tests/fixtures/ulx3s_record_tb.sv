`timescale 1ns/1ps
module ulx3s_record_tb;
    reg clk=0, reset=1;
    always #5 clk=~clk;
    localparam [119:0] GOLD=120'ha55a0100000123456789abcdefa513;
    // CRC values computed independently with Python binascii.crc_hqx, FFFF.
    localparam [119:0] SEQ1=120'ha55a0100010123456789abcdef4e30;
    reg [63:0] tx_record=64'h0123456789abcdef;
    reg tx_record_valid=0, tx_byte_ready=0, rx_byte_valid=0;
    reg [7:0] rx_byte=0;
    reg phy_error=0, rx_record_ready=0;
    wire tx_record_ready,tx_byte_valid,rx_byte_ready,rx_record_valid,fault;
    wire [7:0] tx_byte;
    wire [63:0] rx_record;
    emuflow_gpio_record dut(.*);
    task restart;
        begin
            @(negedge clk); reset=1; rx_byte_valid=0; phy_error=0;
            repeat(3) @(negedge clk);
            reset=0;
        end
    endtask
    task feed(input [119:0] frame);
        integer j;
        begin
            for(j=14;j>=0;j=j-1) begin
                @(negedge clk); rx_byte=frame[j*8+:8]; rx_byte_valid=1;
            end
            @(negedge clk); rx_byte_valid=0;
        end
    endtask
    integer n;
    reg [7:0] held;
    initial begin
        restart();
        tx_record_valid=1; @(negedge clk); tx_record_valid=0;
        for(n=14;n>=0;n=n-1) begin
            if(!tx_byte_valid || tx_byte!==GOLD[n*8+:8]) $fatal(1,"TX golden mismatch");
            held=tx_byte;
            repeat(3) begin
                @(negedge clk);
                if(tx_byte!==held || !tx_byte_valid) $fatal(1,"TX changed while stalled");
            end
            tx_byte_ready=1; @(negedge clk); tx_byte_ready=0;
        end
        if(!tx_record_ready) $fatal(1,"TX did not finish");
        feed(GOLD);
        if(!rx_record_valid || rx_record!==64'h0123456789abcdef || fault)
            $fatal(1,"valid frame rejected");
        repeat(5) @(negedge clk);
        if(!rx_record_valid) $fatal(1,"RX not retained under backpressure");
        rx_record_ready=1; @(negedge clk); rx_record_ready=0;
        feed(GOLD); // valid CRC but replayed sequence
        if(!fault || rx_record_valid) $fatal(1,"duplicate accepted");
        restart(); feed(GOLD ^ 120'h000000000000000000000000000001);
        if(!fault || rx_record_valid) $fatal(1,"corruption accepted");
        restart(); feed(SEQ1);
        if(!fault || rx_record_valid) $fatal(1,"out-of-order accepted");
        restart(); feed(GOLD); feed(SEQ1);
        if(!fault || rx_record_valid) $fatal(1,"overflow not fail-stop");
        restart(); phy_error=1; @(negedge clk); phy_error=0;
        if(!fault || tx_record_ready || rx_byte_ready) $fatal(1,"PHY error ignored");
        repeat(5) @(negedge clk);
        if(!fault) $fatal(1,"fault not sticky");
        restart(); feed(GOLD);
        if(fault || !rx_record_valid) $fatal(1,"reset did not recover");
        $display("PASS record golden CRC, stalls, corruption, sequence, overflow, PHY fault, reset");
        $finish;
    end
    initial begin #100000; $fatal(1,"timeout"); end
endmodule
