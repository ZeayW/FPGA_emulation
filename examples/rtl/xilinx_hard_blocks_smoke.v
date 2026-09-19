module xilinx_hard_blocks_smoke (
    input  wire                 clk,
    input  wire                 we_bram,
    input  wire                 we_uram,
    input  wire signed [26:0]   a,
    input  wire signed [17:0]   b,
    input  wire [9:0]           bram_addr,
    input  wire [31:0]          bram_din,
    input  wire [11:0]          uram_addr,
    input  wire [71:0]          uram_din,
    output reg  signed [44:0]   product,
    output reg  [31:0]          bram_dout,
    output reg  [71:0]          uram_dout
);
    (* use_dsp = "yes" *)
    always @(posedge clk)
        product <= a * b;

    (* ram_style = "block" *) reg [31:0] block_memory [0:1023];
    always @(posedge clk) begin
        if (we_bram)
            block_memory[bram_addr] <= bram_din;
        bram_dout <= block_memory[bram_addr];
    end

    (* ram_style = "ultra" *) reg [71:0] ultra_memory [0:4095];
    always @(posedge clk) begin
        if (we_uram)
            ultra_memory[uram_addr] <= uram_din;
        uram_dout <= ultra_memory[uram_addr];
    end
endmodule
