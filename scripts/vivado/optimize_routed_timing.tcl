if {$argc < 2 || $argc > 6} {
    error "usage: vivado -mode batch -source optimize_routed_timing.tcl -tclargs INPUT_DCP OUTPUT_DIR ?TARGET_WNS_NS? ?PHYS_OPT_DIRECTIVE_OR_OPTION_LIST? ?ROUTE_DIRECTIVE?"
}

set input_dcp [file normalize [lindex $argv 0]]
set output_dir [file normalize [lindex $argv 1]]
set target_wns_ns 0.0
if {$argc >= 3} {
    set target_wns_ns [lindex $argv 2]
}
set phys_opt_options {-directive ExploreWithHoldFix}
if {$argc >= 4} {
    set phys_opt_options [lindex $argv 3]
    if {![string match -* [lindex $phys_opt_options 0]]} {
        set phys_opt_options [list -directive $phys_opt_options]
    }
}
set route_directive {}
if {$argc >= 5} {
    set route_directive [lindex $argv 4]
}
# Forced net replication is a post-place operation in Vivado 2024.2.
# Reject the legacy request before loading a large routed checkpoint.
if {($argc >= 6 && [llength [lindex $argv 5]]) ||
    [lsearch -exact $phys_opt_options -force_replication_on_nets] >= 0} {
    error "forced net replication is unavailable post-route; use post-place replication or post-route -critical_cell_opt/-slr_crossing_opt"
}

# In 2024.2, TNS cleanup accepts the dedicated SLR pass only. Combining it
# with critical-cell optimization produces Vivado_Tcl 4-1061.
if {[lsearch -exact $phys_opt_options -tns_cleanup] >= 0 &&
    [lsearch -exact $phys_opt_options -critical_cell_opt] >= 0} {
    error "post-route TNS cleanup requires a separate -slr_crossing_opt pass; run -critical_cell_opt separately"
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

puts "EMUFLOW_TIMING_OPT_DIRECTIVES phys_opt=$phys_opt_options route=$route_directive"
phys_opt_design {*}$phys_opt_options
write_checkpoint -force "$output_dir/physopt.dcp"
set pending [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
if {$route_directive ne ""} {
    route_design -directive $route_directive
} elseif {[llength $pending]} {
    route_design -preserve
}
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

set unrouted [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
set drc_violations [get_drc_violations -quiet]
set blocking_drc 0
foreach violation $drc_violations {
    if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {incr blocking_drc}
}
set optimized_wns_ns [emuflow_worst_setup_slack]
set hold_path [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type min]
set optimized_whs_ns {}
if {[llength $hold_path]} {set optimized_whs_ns [get_property SLACK $hold_path]}
puts "EMUFLOW_TIMING_OPT_RESULT baseline_cells=[llength $baseline_cells] optimized_cells=[llength $optimized_cells] added_cells=$added_cells missing_cells=$missing_cells changed_cell_types=$changed_cell_types unrouted_nets=[llength $unrouted] drc_violations=[llength $drc_violations] blocking_drc=$blocking_drc optimized_whs_ns=$optimized_whs_ns baseline_wns_ns=$baseline_wns_ns optimized_wns_ns=$optimized_wns_ns target_wns_ns=$target_wns_ns"
# Cell disappearance, replication and primitive changes are normal physical
# optimizations. The inventories above describe transformations; they do not
# impose an object-identity or primitive-preservation acceptance condition.
if {[llength $unrouted] != 0} {
    error "timing optimization left [llength $unrouted] incomplete nets"
}
if {$blocking_drc != 0} {
    error "timing optimization produced $blocking_drc blocking DRC violations"
}
if {$optimized_whs_ns ne "" && $optimized_whs_ns < 0} {
    error "timing optimization hold slack $optimized_whs_ns ns is negative"
}
if {$optimized_wns_ns < $target_wns_ns} {
    error "timing optimization WNS $optimized_wns_ns ns is below target $target_wns_ns ns"
}

puts "EMUFLOW_TIMING_OPT status=pass baseline_cells=[llength $baseline_cells] optimized_cells=[llength $optimized_cells] added_cells=$added_cells missing_cells=$missing_cells changed_cell_types=$changed_cell_types unrouted_nets=0 drc_violations=[llength $drc_violations] blocking_drc=0 optimized_whs_ns=$optimized_whs_ns baseline_wns_ns=$baseline_wns_ns optimized_wns_ns=$optimized_wns_ns"
