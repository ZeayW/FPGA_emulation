module leaf(input a, input unused_input, output [35:0] dec, output y, output z);
  assign dec = {36{a}};
  assign y = dec[0];
  assign z = unused_input;
endmodule

module top(input a, output y, output z);
  leaf u(.a(a), .unused_input(), .dec(), .y(y), .z(z));
endmodule
