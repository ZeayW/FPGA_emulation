// SPDX-License-Identifier: Apache-2.0
//
// Yosys-compatible behavioral replacements for legacy NVDLA simulation
// models.  The upstream NV_DW_lsd model terminates a procedural loop with a
// runtime flag.  That construct is accepted by some commercial frontends but
// is not a synthesizable, statically bounded loop in Yosys.

module NV_DW_lsd (a, dec, enc);
  parameter integer a_width = 8;
  parameter integer b_width = a_width - 1;
  localparam integer enc_width =
      (a_width > 128) ? 8 :
      (a_width > 64)  ? 7 :
      (a_width > 32)  ? 6 :
      (a_width > 16)  ? 5 :
      (a_width > 8)   ? 4 :
      (a_width > 4)   ? 3 :
      (a_width > 2)   ? 2 : 1;

  input wire [a_width-1:0] a;
  output reg [a_width-1:0] dec;
  output reg [enc_width-1:0] enc;

  integer bit_index;
  reg transition_found;

  always @* begin
    enc = a_width - 1;
    transition_found = 1'b0;
    for (bit_index = a_width - 2; bit_index >= 0; bit_index = bit_index - 1) begin
      if (!transition_found && (a[bit_index + 1] != a[bit_index])) begin
        enc = a_width - bit_index - 2;
        transition_found = 1'b1;
      end
    end

    dec = {a_width{1'b0}};
    dec[b_width - enc] = 1'b1;
  end
endmodule
