# Real-device check for native macro expansion and occupied-capacity accounting.
# All products are experiment scratch. This does not qualify NVDLA/global STA.
if {$argc != 2} {error "expected OUTPUT_DIRECTORY PART"}
lassign $argv out part
file mkdir $out
source [file join [file dirname [info script]] .. vivado resource_inventory.tcl]
set stream [open "$out/source.v" w]
puts $stream {
module resource_probe(input clk, input [5:0] data, input [29:0] a,
  input [17:0] b, output o5, output o6, output q, output [47:0] product);
  (* DONT_TOUCH = "yes" *) LUT6_2 #(.INIT(64'h6996966996696996)) dual_lut(
    .I0(data[0]),.I1(data[1]),.I2(data[2]),.I3(data[3]),.I4(data[4]),.I5(data[5]),.O5(o5),.O6(o6));
  (* DONT_TOUCH = "yes" *) FDRE ff(.C(clk),.CE(1'b1),.R(1'b0),.D(o6),.Q(q));
  (* DONT_TOUCH = "yes" *) DSP48E2 dsp(
    .CLK(clk),.A(a),.B(b),.C(48'b0),.D(27'b0),.P(product),
    .ACIN(30'b0),.BCIN(18'b0),.PCIN(48'b0),.CARRYCASCIN(1'b0),.MULTSIGNIN(1'b0),
    .OPMODE(9'b000000101),.ALUMODE(4'b0),.INMODE(5'b0),.CARRYINSEL(3'b0),.CARRYIN(1'b0),
    .CEA1(1'b1),.CEA2(1'b1),.CEB1(1'b1),.CEB2(1'b1),.CEC(1'b1),.CED(1'b1),
    .CEAD(1'b1),.CEALUMODE(1'b1),.CECARRYIN(1'b1),.CECTRL(1'b1),.CEINMODE(1'b1),.CEM(1'b1),.CEP(1'b1),
    .RSTA(1'b0),.RSTB(1'b0),.RSTC(1'b0),.RSTD(1'b0),.RSTCTRL(1'b0),.RSTALUMODE(1'b0),
    .RSTINMODE(1'b0),.RSTALLCARRYIN(1'b0),.RSTM(1'b0),.RSTP(1'b0));
endmodule
}
close $stream
create_project -in_memory -part $part emuflow_resource_probe
set_param general.maxThreads 2
read_verilog "$out/source.v"
synth_design -top resource_probe -part $part -mode out_of_context
create_clock -name probe_clk -period 20 [get_ports clk]
opt_design
place_design -directive Quick
route_design
set incomplete [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
if {[llength $incomplete] != 0} {error "resource probe has incomplete routing"}
set result [emuflow_resource_inventory]
foreach {key expected} {lut 1 ff 1 dsp48 1} {
  if {[dict get $result $key] != $expected} {error "unexpected resource count: $key=[dict get $result $key]"}
}
set stream [open "$out/resources.tsv" w]
puts $stream "metric\tvalue"
dict for {key value} $result {puts $stream "$key\t$value"}
close $stream
report_utilization -file "$out/utilization.rpt"
report_route_status -file "$out/route_status.rpt"
report_drc -file "$out/drc.rpt"
foreach violation [get_drc_violations -quiet] {
  if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {error "resource probe has blocking DRC"}
}
puts "EMUFLOW_RESOURCE_QUALIFICATION status=pass scope=synthetic-occupied-capacity"
exit 0
