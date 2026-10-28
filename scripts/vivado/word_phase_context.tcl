# Conditional timing views of the continuous physical TDM calendar. Call only
# in a fresh, read-only routed snapshot. These annotations are never written
# into an implementation checkpoint or used for infrastructure timing closure.
proc emuflow_word_phase_readonly {} {
  if {![info exists ::emuflow_native_export_stage] || $::emuflow_native_export_stage ne "routed"} {
    error "word phase selection requires a read-only routed export snapshot"
  }
}

proc emuflow_word_phase_bind {path} {
  emuflow_word_phase_readonly
  if {[file size $path]>65536} {error "word phase roles exceed their bounded counter catalog"}
  set input [open $path r]
  try {
    if {[gets $input header]<0 || $header ne "period\tbit\tsource_origin"} {error "invalid word phase role header"}
    set roles [dict create]
    set periods [dict create]
    set pins [dict create]
    while {[gets $input row]>=0} {
      if {[llength [split $row \t]]!=3} {error "invalid word phase role record"}
      lassign [split $row \t] period bit origin
      if {![string is integer -strict $period] || $period<1 || $period>32 ||
          ![string is integer -strict $bit] || $bit<0 || $bit>4 ||
          (($period==1) ? ($bit!=0) : ((1<<$bit)>=$period))} {error "invalid word phase binary coordinate"}
      set key [list $period $bit]
      if {[dict exists $roles $key]} {error "word phase role repeats"}
      dict set periods $period 1
      if {$origin eq "constant:0" && $period==1 && $bit==0} {
        # A source-bound mapped literal supplies the ratio-eight invariant.
        # No vanished register is reconstructed or protected for this role.
        dict set roles $key [list]
        continue
      }
      if {$period==1 || ![regexp {^s[0-9]+$} $origin]} {error "invalid word phase source origin"}
      set cells [get_cells -quiet -hier -filter "EMUFLOW_SOURCE == $origin"]
      if {![llength $cells]} {error "word phase source has no native projection: $origin"}
      set outputs [list]
      foreach cell $cells {
        if {[get_property REF_NAME $cell] ni {FDRE FDSE FDCE FDPE}} {
          error "word phase source needs an explicit native state projection: $origin"
        }
        set clock_pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]
        set clocks [get_clocks -quiet -of_objects $clock_pin]
        if {[llength $clocks]!=1 || [get_property NAME $clocks] ne "emuflow_tdm_clk" ||
            abs([get_property PERIOD $clocks]-5.0)>0.000001} {error "word phase source clock differs"}
        set output [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == Q}]
        if {[llength $output]!=1} {error "word phase source has no unique native Q"}
        set outputs [concat $outputs $output]
        dict set pins [get_property NAME $output] $output
      }
      dict set roles $key $outputs
    }
    if {![dict size $periods]} {error "word phase counter catalog is empty"}
    set repeat 1
    foreach period [dict keys $periods] {
      set width 1
      while {(1<<$width)<$period} {incr width}
      for {set bit 0} {$bit<$width} {incr bit} {
        if {![dict exists $roles [list $period $bit]]} {error "word phase counter bit coverage is incomplete"}
      }
      set a $repeat
      set b $period
      while {$b!=0} {set next [expr {$a%$b}];set a $b;set b $next}
      set repeat [expr {($repeat/$a)*$period}]
    }
  } finally {close $input}
  return [dict create roles $roles pins $pins repeat_words $repeat]
}

proc emuflow_word_phase_select {context phase} {
  emuflow_word_phase_readonly
  if {![string is entier -strict $phase] || $phase<0 || $phase>=[dict get $context repeat_words]} {
    error "word phase is outside the joint serializer recurrence"
  }
  set assignments [dict create]
  dict for {key pins} [dict get $context roles] {
    lassign $key period bit
    set value [expr {(($phase%$period)>>$bit)&1}]
    foreach pin $pins {
      set name [get_property NAME $pin]
      if {[dict exists $assignments $name] && [dict get $assignments $name]!=$value} {
        error "word phase aliases require inconsistent native state values"
      }
      dict set assignments $name $value
    }
  }
  # Validate the whole context before changing any pin. Source replicas and
  # compatible merged aliases receive the same value in this analysis view.
  foreach value {0 1} {
    set selected [list]
    dict for {name bit} $assignments {
      if {$bit==$value} {lappend selected [dict get $context pins $name]}
    }
    if {[llength $selected]} {set_case_analysis $value $selected}
  }
}

proc emuflow_word_phase_clear {context} {
  emuflow_word_phase_readonly
  set pins [dict values [dict get $context pins]]
  if {[llength $pins]} {remove_case_analysis $pins}
}
