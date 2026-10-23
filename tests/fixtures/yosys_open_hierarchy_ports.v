module leaf(input a, input unused_input, output [35:0] dec, output y, output z);
  assign dec = {36{a}};
  assign y = dec[0];
  assign z = unused_input;
endmodule

module constant_driver(output zero, output [1:0] pattern);
  assign zero = 1'b0;
  assign pattern = 2'b10;
endmodule

module constant_mid(output zero, output [1:0] pattern);
  constant_driver u(.zero(zero), .pattern(pattern));
endmodule

module top(input a, output y, output z, output zero, output [1:0] pattern);
  leaf u(.a(a), .unused_input(), .dec(), .y(y), .z(z));
  constant_mid constants(.zero(zero), .pattern(pattern));
endmodule
