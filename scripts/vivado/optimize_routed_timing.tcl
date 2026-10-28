if {$argc < 2 || $argc > 6} {
    error "usage: vivado -mode batch -source optimize_routed_timing.tcl -tclargs INPUT_DCP OUTPUT_DIR ?TARGET_WNS_NS? ?PHYS_OPT_DIRECTIVE? ?ROUTE_DIRECTIVE? ?FORCE_REPLICATION_NET_LIST?"
}

set input_dcp [file normalize [lindex $argv 0]]
set output_dir [file normalize [lindex $argv 1]]
set target_wns_ns 0.0
if {$argc >= 3} {
    set target_wns_ns [lindex $argv 2]
}
set phys_opt_directive AggressiveFanoutOpt
if {$argc >= 4} {
    set phys_opt_directive [lindex $argv 3]
}
set route_directive AggressiveExplore
if {$argc >= 5} {
    set route_directive [lindex $argv 4]
}
set force_replication_net_names {}
if {$argc >= 6} {
    set force_replication_net_names [lindex $argv 5]
}

if {![file isfile $input_dcp]} {
    error "input checkpoint does not exist: $input_dcp"
}
file mkdir $output_dir
open_checkpoint $input_dcp

proc emuflow_worst_setup_slack {} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]
    if {[llength $paths] != 1} {
        error "design did not produce exactly one worst setup path"
    }
    return [get_property SLACK $paths]
}

set baseline_cells [get_cells -hier -filter {REF_NAME != GND && REF_NAME != VCC}]
array set baseline_ref_by_name {}
foreach name [get_property NAME $baseline_cells] \
        ref_name [get_property REF_NAME $baseline_cells] {
    if {[info exists baseline_ref_by_name($name)]} {
        error "baseline checkpoint contains duplicate cell name $name"
    }
    set baseline_ref_by_name($name) $ref_name
}
set baseline_wns_ns [emuflow_worst_setup_slack]
report_timing_summary -file "$output_dir/baseline_timing_summary.rpt"
puts "EMUFLOW_TIMING_OPT_BASELINE cells=[llength $baseline_cells] wns_ns=$baseline_wns_ns"

set force_replication_nets {}
foreach name $force_replication_net_names {
    set matches [get_nets -quiet -hier -filter "NAME == $name"]
    if {[llength $matches] != 1} {
        error "forced-replication net $name resolved to [llength $matches] nets"
    }
    lappend force_replication_nets [lindex $matches 0]
}
puts "EMUFLOW_TIMING_OPT_DIRECTIVES phys_opt=$phys_opt_directive route=$route_directive forced_replication_nets=[llength $force_replication_nets]"
if {[llength $force_replication_nets] != 0} {
    phys_opt_design -force_replication_on_nets $force_replication_nets
}
phys_opt_design -directive $phys_opt_directive
write_checkpoint -force "$output_dir/physopt.dcp"
route_design -directive $route_directive
write_checkpoint -force "$output_dir/optimized_routed.dcp"
report_route_status -file "$output_dir/route_status.rpt"
report_drc -file "$output_dir/drc.rpt"
report_timing_summary -file "$output_dir/timing_summary.rpt"

set optimized_cells [get_cells -hier -filter {REF_NAME != GND && REF_NAME != VCC}]
array set optimized_ref_by_name {}
foreach name [get_property NAME $optimized_cells] \
        ref_name [get_property REF_NAME $optimized_cells] {
    if {[info exists optimized_ref_by_name($name)]} {
        error "optimized checkpoint contains duplicate cell name $name"
    }
    set optimized_ref_by_name($name) $ref_name
}

set missing_file [open "$output_dir/missing_cells.tsv" w]
puts $missing_file "name\tbaseline_ref_name"
set missing_cells 0
foreach name [lsort -ascii [array names baseline_ref_by_name]] {
    if {![info exists optimized_ref_by_name($name)]} {
        puts $missing_file "$name\t$baseline_ref_by_name($name)"
        incr missing_cells
    }
}
close $missing_file

set added_file [open "$output_dir/added_cells.tsv" w]
puts $added_file "name\tref_name"
set added_cells 0
foreach name [lsort -ascii [array names optimized_ref_by_name]] {
    if {![info exists baseline_ref_by_name($name)]} {
        puts $added_file "$name\t$optimized_ref_by_name($name)"
        incr added_cells
    }
}
close $added_file

set changed_file [open "$output_dir/changed_cell_types.tsv" w]
puts $changed_file "name\tbaseline_ref_name\toptimized_ref_name"
set changed_cell_types 0
foreach name [lsort -ascii [array names baseline_ref_by_name]] {
    if {[info exists optimized_ref_by_name($name)] &&
        $baseline_ref_by_name($name) ne $optimized_ref_by_name($name)} {
        puts $changed_file \
            "$name\t$baseline_ref_by_name($name)\t$optimized_ref_by_name($name)"
        incr changed_cell_types
    }
}
close $changed_file

set unrouted [get_nets -quiet -filter {ROUTE_STATUS == UNROUTED}]
set drc_violations [get_drc_violations -quiet]
set optimized_wns_ns [emuflow_worst_setup_slack]
puts "EMUFLOW_TIMING_OPT_RESULT baseline_cells=[llength $baseline_cells] optimized_cells=[llength $optimized_cells] added_cells=$added_cells missing_cells=$missing_cells changed_cell_types=$changed_cell_types unrouted_nets=[llength $unrouted] drc_violations=[llength $drc_violations] baseline_wns_ns=$baseline_wns_ns optimized_wns_ns=$optimized_wns_ns target_wns_ns=$target_wns_ns"
# Cell disappearance, replication and primitive changes are normal physical
# optimizations. The inventories above describe transformations; they do not
# impose an object-identity or primitive-preservation acceptance condition.
if {[llength $unrouted] != 0} {
    error "timing optimization left [llength $unrouted] unrouted nets"
}
if {[llength $drc_violations] != 0} {
    error "timing optimization produced [llength $drc_violations] DRC violations"
}
if {$optimized_wns_ns < $target_wns_ns} {
    error "timing optimization WNS $optimized_wns_ns ns is below target $target_wns_ns ns"
}

puts "EMUFLOW_TIMING_OPT status=pass baseline_cells=[llength $baseline_cells] optimized_cells=[llength $optimized_cells] added_cells=$added_cells missing_cells=$missing_cells changed_cell_types=$changed_cell_types unrouted_nets=0 drc_violations=0 baseline_wns_ns=$baseline_wns_ns optimized_wns_ns=$optimized_wns_ns"
