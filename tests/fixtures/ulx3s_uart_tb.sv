`timescale 1ns/1ps
module ulx3s_uart_tb # (parameter real RX_HALF_PERIOD=20.2);
    reg aclk=0, bclk=0, reset=1;
    always #20 aclk=~aclk;
    // Deliberately asynchronous phase and 1% frequency offset, not a shared clock.
    initial begin #7; forever #(RX_HALF_PERIOD) bclk=~bclk; end
    reg [7:0] tx_data=0;
    reg tx_valid=0, rx_ready=1;
    wire tx_ready, line, rx_valid;
    wire [7:0] rx_data;
    wire frame_error, overrun;
    reg fault=0;
    emuflow_gpio_uart sender(.clk(aclk), .reset(reset), .serial_rx(1'b1),
        .serial_tx(line), .tx_data(tx_data), .tx_valid(tx_valid), .tx_ready(tx_ready),
        .rx_data(), .rx_valid(), .rx_ready(1'b1), .framing_error(), .overrun());
    emuflow_gpio_uart receiver(.clk(bclk), .reset(reset), .serial_rx(fault ? 1'b0 : line),
        .serial_tx(), .tx_data(8'b0), .tx_valid(1'b0), .tx_ready(),
        .rx_data(rx_data), .rx_valid(rx_valid), .rx_ready(rx_ready),
        .framing_error(frame_error), .overrun(overrun));
    integer received=0, errors=0, overruns=0;
    reg check_stream=1;
    always @(posedge bclk) if (!reset) begin
        if (frame_error) errors=errors+1;
        if (overrun) overruns=overruns+1;
        if (rx_valid && rx_ready && check_stream) begin
            if (rx_data !== (received & 255)) $fatal(1,"data/order error at %0d",received);
            received=received+1;
        end
    end
    task send(input [7:0] value);
        begin
            @(negedge aclk);
            while (!tx_ready) @(negedge aclk);
            tx_data=value; tx_valid=1;
            @(negedge aclk); tx_valid=0;
        end
    endtask
    integer i;
    initial begin
        #300; @(negedge aclk); reset=0;
        for(i=0;i<256;i=i+1) send(i);
        wait(received==256);
        if(errors || overruns) $fatal(1,"unexpected link error");
        @(negedge bclk); check_stream=0; rx_ready=0;
        send(8'ha5); wait(rx_valid);
        send(8'h5a); wait(tx_ready); repeat(20) @(negedge bclk);
        if(rx_data !== 8'ha5 || !rx_valid || overruns!=1)
            $fatal(1,"backpressure must preserve pending data and report overflow");
        rx_ready=1; repeat(5) @(negedge bclk);
        fault=1; repeat(200) @(negedge bclk); fault=0;
        repeat(20) @(negedge bclk);
        if(errors!=1 || rx_valid) $fatal(1,"break must report framing error without data");
        send(8'hc3); wait(rx_valid);
        if(rx_data !== 8'hc3) $fatal(1,"failed to recover after break");
        $display("PASS 256-byte asynchronous stream, backpressure, overflow, break recovery");
        $finish;
    end
    initial begin #4000000; $fatal(1,"timeout"); end
endmodule
