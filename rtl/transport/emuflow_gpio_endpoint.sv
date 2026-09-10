// SPDX-License-Identifier: Apache-2.0
// Byte PHY + checked records. Fault stops traffic until coordinated reset.
module emuflow_gpio_endpoint #(
    parameter integer CLOCKS_PER_BIT=16
) (
    input wire clk, reset,
    input wire serial_rx,
    output wire serial_tx,
    input wire [63:0] tx_record,
    input wire tx_valid,
    output wire tx_ready,
    output wire [63:0] rx_record,
    output wire rx_valid,
    input wire rx_ready,
    output wire fault
);
    wire [7:0] tx_byte, rx_byte;
    wire tx_byte_valid, tx_byte_ready, rx_byte_valid, rx_byte_ready;
    wire framing_error, overrun;
    emuflow_gpio_uart #(.CLOCKS_PER_BIT(CLOCKS_PER_BIT)) phy (
        .clk(clk), .reset(reset || fault), .serial_rx(serial_rx), .serial_tx(serial_tx),
        .tx_data(tx_byte), .tx_valid(tx_byte_valid), .tx_ready(tx_byte_ready),
        .rx_data(rx_byte), .rx_valid(rx_byte_valid), .rx_ready(rx_byte_ready),
        .framing_error(framing_error), .overrun(overrun));
    emuflow_gpio_record records (
        .clk(clk), .reset(reset), .tx_record(tx_record), .tx_record_valid(tx_valid),
        .tx_record_ready(tx_ready), .tx_byte(tx_byte), .tx_byte_valid(tx_byte_valid),
        .tx_byte_ready(tx_byte_ready), .rx_byte(rx_byte), .rx_byte_valid(rx_byte_valid),
        .rx_byte_ready(rx_byte_ready), .phy_error(framing_error || overrun),
        .rx_record(rx_record), .rx_record_valid(rx_valid),
        .rx_record_ready(rx_ready), .fault(fault));
endmodule
