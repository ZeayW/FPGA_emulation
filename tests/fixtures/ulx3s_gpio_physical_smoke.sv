// Physical tool bring-up only; this is not the cross-board transport protocol.
module ulx3s_gpio_physical_smoke (
    input wire clk_25mhz,
    input wire reset_n,
    input wire link_rx,
    output reg link_tx
);
    // Asynchronous assertion and synchronized deassertion for the local domain.
    reg [1:0] reset_pipe;
    always @(posedge clk_25mhz or negedge reset_n)
        if (!reset_n) reset_pipe <= 2'b00;
        else reset_pipe <= {reset_pipe[0], 1'b1};
    reg rx_meta, rx_sync;
    reg [7:0] counter;
    always @(posedge clk_25mhz) begin
        if (!reset_pipe[1]) begin
            rx_meta <= 0;
            rx_sync <= 0;
            counter <= 0;
            link_tx <= 0;
        end else begin
            rx_meta <= link_rx;
            rx_sync <= rx_meta;
            counter <= counter + 1'b1;
            link_tx <= counter[7] ^ rx_sync;
        end
    end
endmodule
