module leaf(input a, input unused_input, output [35:0] dec, output y);
  assign dec = {36{a}};
  assign y = dec[0];
endmodule

module top(input a, output y);
  leaf u(.a(a), .unused_input(), .dec(), .y(y));
endmodule
