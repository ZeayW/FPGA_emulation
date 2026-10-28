# Read-only capture-family timing extraction. A clock family includes native
# sequential checks and output ports with an explicit output-delay clock.
# The caller loads and seals that I/O environment before using this view.
source [file join [file dirname [info script]] native_word_timing.tcl]

proc emuflow_native_word_capture_costs {sources stream inputs progress} {
  emuflow_word_phase_readonly
  set count 0
  foreach capture [dict get $sources dut_clocks] {
    foreach capture_edge {rise fall} {
      set query "dut-capture:[get_property NAME $capture]:$capture_edge"
      foreach launch [dict get $sources dut_clocks] {
        foreach launch_edge {rise fall} {
          set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
              -${launch_edge}_from [list $launch] -${capture_edge}_to [list $capture]]
          emuflow_native_word_cost_row $stream -1 $query dut-clock \
              [get_property NAME $launch] $launch_edge $paths
          incr count
        }
      }
      dict for {origin pins} [dict get $sources rx] {
        set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
            -from $pins -${capture_edge}_to [list $capture]]
        emuflow_native_word_cost_row $stream -1 $query rx $origin "" $paths
        incr count
        if {$count%100==0} {
          flush $stream
          puts $progress "[binary encode hex [encoding convertto utf-8 $query]]\t$count"
          flush $progress
        }
      }
      dict for {name port} $inputs {
        set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
            -from $port -${capture_edge}_to [list $capture]]
        emuflow_native_word_cost_row $stream -1 $query declared-input $name "" $paths
        incr count
      }
      flush $stream
      puts $progress "[binary encode hex [encoding convertto utf-8 $query]]\t$count"
      flush $progress
    }
  }
  # Family maxima do not supply per-endpoint TNS or prove source coverage.
  return $count
}

proc emuflow_native_word_state_hold {sources stream} {
  emuflow_word_phase_readonly
  set starts [dict create]
  foreach clock [dict get $sources dut_clocks] {
    foreach pin [all_registers -clock [get_property NAME $clock] -clock_pins] {
      dict set starts [get_property NAME $pin] $pin
    }
  }
  set cells [dict keys [dict get $sources dut_cells]]
  set ends [list]
  if {[llength $cells]} {
    set ends [get_pins -quiet -of_objects [get_cells -quiet $cells] \
        -filter {DIRECTION == IN && IS_CLOCK == 0}]
  }
  puts $stream "scope\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex"
  set paths [list]
  if {[dict size $starts] && [llength $ends]} {
    set paths [get_timing_paths -quiet -delay_type min -max_paths 1 -nworst 1 \
        -from [dict values $starts] -to $ends]
  }
  if {[llength $paths]} {
    puts $stream "dut-state-to-state\tnative-min-path\t[get_property SLACK $paths]\t[get_property DATAPATH_DELAY $paths]\t[binary encode hex [encoding convertto utf-8 [get_property STARTPOINT_PIN $paths]]]\t[binary encode hex [encoding convertto utf-8 [get_property ENDPOINT_PIN $paths]]]"
  } else {
    puts $stream "dut-state-to-state\tnative-no-timed-path\t\t\t\t"
  }
  # Excludes clocked primary inputs and outputs. It does not characterize their
  # minimum external delays or the next logical version across a TDM link.
  return [llength $paths]
}

# RX data launches are excluded from next-word setup checks because their
# destination occurrence belongs to the schedule. Physical minimum checks
# still apply to those launches and must retain the actual native clocks.
# Call on an unconditional snapshot, before reference or phase annotations.
proc emuflow_native_word_rx_infrastructure_hold {sources stream} {
  emuflow_word_phase_readonly
  set starts [dict get $sources rx_clock_pins]
  set cells [dict keys [dict get $sources infrastructure_cells]]
  set ends [list]
  if {[llength $cells]} {
    set ends [get_pins -quiet -of_objects [get_cells -quiet $cells] \
        -filter {DIRECTION == IN && IS_CLOCK == 0}]
  }
  puts $stream "scope\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex\tstart_clock_hex\tend_clock_hex"
  set paths [list]
  if {[llength $starts] && [llength $ends]} {
    set paths [get_timing_paths -quiet -delay_type min -max_paths 1 -nworst 1 \
        -from $starts -to $ends]
  }
  if {[llength $paths]} {
    set fields [list rx-to-infrastructure native-min-path \
        [get_property SLACK $paths] [get_property DATAPATH_DELAY $paths]]
    foreach property {STARTPOINT_PIN ENDPOINT_PIN STARTPOINT_CLOCK ENDPOINT_CLOCK} {
      lappend fields [binary encode hex [encoding convertto utf-8 [get_property $property $paths]]]
    }
    puts $stream [join $fields \t]
  } else {
    puts $stream "rx-to-infrastructure\tnative-no-timed-path\t\t\t\t\t\t"
  }
  # Missing source roles remain coverage gaps in the source catalog. An empty
  # query does not certify their elimination or any uncharacterized I/O hold.
  return [llength $paths]
}
