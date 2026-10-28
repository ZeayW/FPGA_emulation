# Native structural constants; no case values, numerical arcs or original identities.
proc emuflow_source_hex {v} {binary encode hex [encoding convertto utf-8 $v]}
proc emuflow_source_literal {s} {return [string map {\\ \\\\ \" \\\"} $s]}
proc emuflow_source_fact {stream kind name property value} {
 puts $stream "$kind\t[emuflow_source_hex $name]\t$property\t[emuflow_source_hex $value]"
}
proc emuflow_source_pin_properties {stream pin} {
 if {[llength $pin]!=1} {return}
 set name [get_property NAME $pin]
 emuflow_source_fact $stream pin $name REF_PIN_NAME [get_property REF_PIN_NAME $pin]
 emuflow_source_fact $stream pin $name DIRECTION [get_property DIRECTION $pin]
 # TYPE is the native net's constant classification. Do not enumerate
 # all properties or expand a global GND/VCC net into every hierarchy alias.
 set nets [get_nets -quiet -of_objects $pin]
 set names [list]
 foreach net $nets {
  set n [get_property NAME $net];lappend names $n
  emuflow_source_fact $stream net $n TYPE [get_property TYPE $net]
 }
 emuflow_source_fact $stream connection $name net_names [join $names \n]
 if {![llength $nets]} {emuflow_source_fact $stream connection $name disposition no-connected-net}
}

proc emuflow_native_source_properties {requested output} {
  set stream [open $output w]
  puts $stream "kind\tobject_hex\tproperty\tvalue_hex"
  set owners [dict create]
  dict for {name _} $requested {
   set slash [string last / $name]
   if {$slash<0} {emuflow_source_fact $stream query $name disposition no-containing-cell;continue}
   set owner [string range $name 0 [expr {$slash-1}]]
   if {![dict exists $owners $owner]} {
    set cells [get_cells -quiet -hier -filter "NAME == \"[emuflow_source_literal $owner]\""]
    dict set owners $owner $cells
    emuflow_source_fact $stream cell $owner matches [llength $cells]
    if {[llength $cells]==1} {
     set ref [get_property REF_NAME $cells];emuflow_source_fact $stream cell $owner REF_NAME $ref
     if {$ref eq "DSP_A_B_DATA"} {
      foreach p {AREG BREG A_INPUT B_INPUT} {emuflow_source_fact $stream cell $owner $p [get_property $p $cells]}
     } elseif {$ref eq "CARRY8"} {emuflow_source_fact $stream cell $owner CARRY_TYPE [get_property CARRY_TYPE $cells]}
     set inputs [get_pins -quiet -of_objects $cells -filter {DIRECTION == IN}]
     foreach pin $inputs {
      set p [get_property REF_PIN_NAME $pin]
      if {($ref eq "DSP_A_B_DATA" && [regexp {^(A|B|ACIN|BCIN)\[|^CLK$|^RST[AB]$|^CE[AB][12]$} $p]) ||
          ($ref in {CARRY4 CARRY8} && [regexp {^(S|DI)\[|^CI$|^CI_TOP$} $p])} {emuflow_source_pin_properties $stream $pin}
     }
    }
   }
   set cells [dict get $owners $owner]
   if {[llength $cells]==1} {
    set refpin [string range $name [expr {$slash+1}] end]
    set pins [get_pins -quiet -of_objects $cells -filter "REF_PIN_NAME == \"[emuflow_source_literal $refpin]\""]
    emuflow_source_fact $stream query $name pin_matches [llength $pins]
    emuflow_source_pin_properties $stream $pins
   } else {
    emuflow_source_fact $stream query $name pin_matches 0
   }
  }
  close $stream
  return [dict size $requested]
}
