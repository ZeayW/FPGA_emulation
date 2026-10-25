"""Exercise physical alias and generated-clock rejection at the Tcl boundary."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


def tcl(script):
    script='if {[catch {\n'+script+'\n} message options]} {puts stderr [dict get $options -errorinfo]; exit 1}\n'
    result=subprocess.run(['tclsh'],input=script,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stderr=='',result.stderr
    assert result.stdout.strip().endswith('pass'),result.stdout


@pytest.mark.skipif(not shutil.which('tclsh'),reason='Tcl interpreter required')
def test_native_word_aliases_resolve_via_their_own_physical_lane(tmp_path):
    mapping=tmp_path/'map.tsv'
    rows=[('tx0','word-tx','bank/native_phy/word0[0]','pad0'),
          ('tx1','word-tx','bank/native_phy/word1[0]','pad1'),
          ('rx','word-rx','bank/rx_words[0]','rx_pad')]
    mapping.write_text('logical_hex\tkind\tphysical_hex\tanchor_hex\n'+''.join(
        '\t'.join(v.encode().hex()if i!=1 else v for i,v in enumerate(row))+'\n'for row in rows))
    script=r'''
set types {buf0 OBUFDS buf1 OBUFDS bank/native_phy/ser0 TX_BITSLICE bank/native_phy/ser1 TX_BITSLICE bank/rx_reg FDRE}
set directions {buf0/O OUT buf1/O OUT buf0/I IN buf1/I IN bank/native_phy/ser0/O OUT bank/native_phy/ser1/O OUT bank/native_phy/ser0/D[0] IN bank/native_phy/ser1/D[0] IN bank/rx_reg/Q OUT}
set nets {pad0 {pad_net0} pad1 {pad_net1} buf0/I {serial0} buf1/I {serial1} bank/native_phy/word0[0] {payload} bank/native_phy/word1[0] {payload} bank/native_phy/ser0/D[0] {payload} bank/native_phy/ser1/D[0] {payload} bank/rx_words[0] {received} bank/rx_reg/Q {received}}
set pins {pad_net0 {buf0/O} pad_net1 {buf1/O} serial0 {bank/native_phy/ser0/O} serial1 {bank/native_phy/ser1/O} payload {bank/native_phy/ser0/D[0] bank/native_phy/ser1/D[0]} received {bank/rx_reg/Q} buf0 {buf0/I buf0/O} buf1 {buf1/I buf1/O} bank/native_phy/ser0 {bank/native_phy/ser0/D[0]} bank/native_phy/ser1 {bank/native_phy/ser1/D[0]}}
proc get_property {property object} {
 global types directions
 set object [lindex $object 0]
 if {$property eq "NAME"} {return $object}
 if {$property eq "REF_NAME"} {return [dict get $types $object]}
 if {$property eq "REF_PIN_NAME"} {return [lindex [split $object /] end]}
 error "unexpected property $property"
}
proc get_ports {args} {return [lindex $args end]}
proc get_nets {args} {global nets;return [dict get $nets [lindex [lindex $args end] 0]]}
proc get_cells {args} {
 global types
 set objects [lindex $args [expr {[lsearch -exact $args -of_objects]+1}]]
 set filter [lindex $args [expr {[lsearch -exact $args -filter]+1}]]
 set result {}
 foreach pin $objects {
  set cell [join [lrange [split $pin /] 0 end-1] /]
  if {[llength $args]==3 || [dict get $types $cell] eq [lindex $filter 2]} {lappend result $cell}
 }
 return [lsort -unique $result]
}
proc get_pins {args} {
 global pins directions
 if {[lsearch -exact $args -hier]>=0} {return [lindex $args end]}
 set key [lindex [lindex $args [expr {[lsearch -exact $args -of_objects]+1}]] 0]
 set result [dict get $pins $key]
 if {[lsearch -exact $args -filter]>=0} {
  set direction [lindex [lindex $args end] 2];set selected {}
  if {[string match REF_PIN_NAME* [lindex $args end]]} {
   foreach pin $result {if {[lindex [split $pin /] end] eq $direction} {lappend selected $pin}}
  } else {
   foreach pin $result {if {[dict get $directions $pin] eq $direction} {lappend selected $pin}}
  }
  return $selected
 }
 return $result
}
set mapping [emuflow_load_fixed_io_map @MAP@]
if {[get_property NAME [emuflow_fixed_io_object $mapping tx0]] ne {bank/native_phy/ser0/D[0]}} {error "wrong lane zero"}
if {[get_property NAME [emuflow_fixed_io_object $mapping tx1]] ne {bank/native_phy/ser1/D[0]}} {error "alias chose another lane"}
if {[get_property NAME [emuflow_fixed_io_object $mapping rx]] ne {bank/rx_reg/Q}} {error "wrong receive launch"}
set ::emuflow_fixed_io_cache {}
dict set nets {bank/native_phy/ser1/D[0]} {unrelated}
if {![catch {emuflow_fixed_io_object $mapping tx1}]} {error "unrelated serializer data accepted"}
dict set types bank/rx_reg LUT1
if {![catch {emuflow_fixed_io_object $mapping rx}]} {error "unregistered receive output accepted"}
puts pass
'''.replace('@MAP@','{'+str(mapping)+'}')
    tcl('source {'+str(ROOT/'scripts/vivado/timing_contract.tcl')+'}\n'+script)


@pytest.mark.skipif(not shutil.which('tclsh'),reason='Tcl interpreter required')
def test_generated_clocks_must_reach_a_declared_root_without_cycles(tmp_path):
    path=tmp_path/'clocks.tsv'
    path.write_text('clock_hex\tport_hex\tperiod_ns\n'+'fabric'.encode().hex()+'\t'+'fabric_clk'.encode().hex()+'\t5\n')
    script=r'''
set clocks {fabric generated1 generated2}
set masters {generated1 fabric generated2 generated1}
proc get_ports {args} {return [lindex $args end]}
proc get_clocks {args} {
 global clocks
 if {[llength $args]==1} {return $clocks}
 if {[lsearch -exact $args -of_objects]>=0} {return fabric}
 set name [lindex $args end]
 if {[lsearch -exact $clocks $name]>=0} {return $name}
 return {}
}
proc get_property {property object} {
 global masters
 switch $property {
  NAME {return $object}
  PERIOD {return 5}
  IS_GENERATED {return [expr {$object ne "fabric" && $object ne "unexpected"}]}
  MASTER_CLOCK {return [dict get $masters $object]}
 }
 error "unexpected property"
}
emuflow_validate_clock_contract @TABLE@ true
if {![catch {emuflow_validate_clock_contract @TABLE@}]} {error "legacy mode admitted extra clocks"}
dict set masters generated2 missing
if {![catch {emuflow_validate_clock_contract @TABLE@ true}]} {error "missing master admitted"}
dict set masters generated2 generated1;dict set masters generated1 generated2
if {![catch {emuflow_validate_clock_contract @TABLE@ true}]} {error "clock cycle admitted"}
dict set masters generated1 fabric;lappend clocks unexpected
if {![catch {emuflow_validate_clock_contract @TABLE@ true}]} {error "unexpected primary admitted"}
puts pass
'''.replace('@TABLE@','{'+str(path)+'}')
    tcl('source {'+str(ROOT/'scripts/vivado/native_session.tcl')+'}\n'+script)
