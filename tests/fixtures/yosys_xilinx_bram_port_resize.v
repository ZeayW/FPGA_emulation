// Minimal public primitive-signature regression, not a physical platform.
// Port widths match engines/yosys/techlibs/xilinx/cells_xtra.v RAMB18E2.
(* blackbox *)
module RAMB18E2(input [1:0] WEA, input [3:0] WEBWE, output [15:0] DOUTADOUT);
endmodule

module bram_port_resize(input [3:0] we, output [31:0] data);
  // The common HALF/FULL techmap supplies four WEA bits to both variants.
  RAMB18E2 tdp(.WEA(we), .WEBWE(4'b0000), .DOUTADOUT(data));
  // The SDP techmap uses an unsized zero (32 bits) for unused WEA.
  RAMB18E2 sdp(.WEA(0), .WEBWE(we));
endmodule
