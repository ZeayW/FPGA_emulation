// SPDX-License-Identifier: Apache-2.0
// 8N1 byte PHY for independently clocked boards. No reliability/commit claim:
// upper layers must frame/check/retry and stop on framing_error or overrun.
module emuflow_gpio_uart #(
    parameter integer CLOCKS_PER_BIT = 16
) (
    input wire clk,
    input wire reset,
    input wire serial_rx,
    output wire serial_tx,
    input wire [7:0] tx_data,
    input wire tx_valid,
    output wire tx_ready,
    output reg [7:0] rx_data,
    output reg rx_valid,
    input wire rx_ready,
    output reg framing_error,
    output reg overrun
);
    // A whole-frame test must cover the permitted oscillator mismatch.
    // This is not an auto-baud or arbitrary-rate asynchronous interface.
    // synthesis translate_off
    initial if (CLOCKS_PER_BIT < 8) $fatal(1, "CLOCKS_PER_BIT must be >= 8");
    // synthesis translate_on
    localparam integer CW = $clog2(CLOCKS_PER_BIT);
    reg [CW-1:0] tx_count;
    reg [3:0] tx_bit;
    reg [9:0] tx_shift;
    reg tx_busy;
    assign tx_ready = !tx_busy;
    assign serial_tx = tx_busy ? tx_shift[0] : 1'b1;
    always @(posedge clk) begin
        if (reset) begin
            tx_count <= 0;
            tx_bit <= 0;
            tx_shift <= 10'h3ff;
            tx_busy <= 0;
        end else if (!tx_busy) begin
            if (tx_valid) begin
                tx_shift <= {1'b1, tx_data, 1'b0};
                tx_count <= CLOCKS_PER_BIT - 1;
                tx_bit <= 0;
                tx_busy <= 1;
            end
        end else if (tx_count != 0) tx_count <= tx_count - 1'b1;
        else begin
            tx_count <= CLOCKS_PER_BIT - 1;
            tx_shift <= {1'b1, tx_shift[9:1]};
            if (tx_bit == 9) tx_busy <= 0;
            else tx_bit <= tx_bit + 1'b1;
        end
    end

    // Only the first stage samples the external wire; never consume it in
    // parallel elsewhere. Physical CDC qualification must inspect this chain.
    (* async_reg = "true" *) reg rx_meta, rx_sync;
    always @(posedge clk) begin
        if (reset) begin rx_meta <= 1; rx_sync <= 1; end
        else begin rx_meta <= serial_rx; rx_sync <= rx_meta; end
    end
    localparam IDLE=0, START=1, DATA=2, STOP=3, BREAK_WAIT=4;
    reg [2:0] state;
    reg [CW-1:0] rx_count;
    reg [2:0] rx_bit;
    reg [7:0] rx_shift;
    always @(posedge clk) begin
        if (reset) begin
            state <= IDLE;
            rx_count <= 0;
            rx_bit <= 0;
            rx_shift <= 0;
            rx_data <= 0;
            rx_valid <= 0;
            framing_error <= 0;
            overrun <= 0;
        end else begin
            framing_error <= 0;
            overrun <= 0;
            if (rx_valid && rx_ready) rx_valid <= 0;
            case (state)
                IDLE: if (!rx_sync) begin
                    rx_count <= CLOCKS_PER_BIT / 2 - 1;
                    state <= START;
                end
                START: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                else if (rx_sync) state <= IDLE; // rejected short start glitch
                else begin
                    rx_count <= CLOCKS_PER_BIT - 1;
                    rx_bit <= 0;
                    state <= DATA;
                end
                DATA: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                else begin
                    rx_shift[rx_bit] <= rx_sync;
                    rx_count <= CLOCKS_PER_BIT - 1;
                    if (rx_bit == 7) state <= STOP;
                    else rx_bit <= rx_bit + 1'b1;
                end
                STOP: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                else if (!rx_sync) begin
                    framing_error <= 1;
                    state <= BREAK_WAIT;
                end else begin
                    if (rx_valid && !rx_ready) overrun <= 1;
                    else begin rx_data <= rx_shift; rx_valid <= 1; end
                    state <= IDLE;
                end
                BREAK_WAIT: if (rx_sync) state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end
endmodule
