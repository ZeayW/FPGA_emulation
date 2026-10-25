import csv
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json
from emuflow.vivado_session import (
    SCHEMA, prepare_frontiers, confirm_frontiers, prepare_timing, prepare_manifest, _tcl_word,
)
from emuflow.vivado_optimization import OptimizationProofView
from emuflow.vivado_physical import validate_synthesized_continuation
from tests.test_vivado_constant_frontier import conditional_model

ROOT = Path(__file__).resolve().parents[1]

def _tcl86():
    for candidate in (shutil.which("tclsh8.6"),shutil.which("tclsh9.0"),shutil.which("tclsh")):
        if candidate:
            version = subprocess.run([candidate],input="puts [info patchlevel]\n",text=True,capture_output=True,check=True).stdout.strip()
            if tuple(int(v) for v in version.split('.')[:2]) >= (8,6):
                return candidate
    return None

TCL86 = _tcl86()



def session_fixture(tmp_path, *, require_constant=True, dependent=False):
    mapped, functions, _, inventory, facts, digest, origins = conditional_model(tmp_path, dependent=dependent)
    out = tmp_path/"native";out.mkdir()
    (out/"synthesized.dcp").write_bytes(b"checked synthetic native snapshot")
    shutil.copyfile(inventory, out/"mapped_cells.tsv")
    with inventory.open() as source, (out/"routed_mapped_cells.tsv").open("w") as target:
        rows = csv.reader(source, delimiter="\t")
        writer = csv.writer(target, delimiter="\t", lineterminator="\n")
        writer.writerow(next(rows))
        for index, row in enumerate(rows):
            writer.writerow(row[:4]+[f"SLICE_X{index}Y0", "AFF", str(index), "0", "LEAF"])
    query = tmp_path/"queries.tsv"
    end = "conditional/D" if require_constant else "front/I1"
    query.write_text("endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\n"+
        "\t".join(["path0".encode().hex(), "local", "pin", "kept/Q".encode().hex(), "pin", end.encode().hex(), "", ""])+"\n")
    session = dict(schema=SCHEMA, source_preflight_stage="synthesized", fpga="fpga0", part="xcvu19p-fsva3824-2-e", output=str(out),
        mapped_index=str(mapped), functions=str(functions), mapped_index_sha256=digest,
        synthesized_checkpoint=str(out/"synthesized.dcp"), synthesized_checkpoint_sha256=file_sha256(out/"synthesized.dcp"),
        queries={"local_path_timing": str(query)})
    return session, facts, origins


@pytest.mark.parametrize("dependent", [False, True])
def test_source_candidates_native_facts_and_routed_proof_are_one_checked_pipeline(tmp_path, dependent):
    session, facts, origins = session_fixture(tmp_path, dependent=dependent)
    out = Path(session["output"])
    result = prepare_frontiers(session)
    assert result["constant_sources"] == (2 if dependent else 1)
    assert result["native_frontiers"] == 1
    assert not (out/"synthesized-lineage.sqlite").exists()
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    assert confirm_frontiers(session)["confirmed_sources"] == result["constant_sources"]
    assert prepare_timing(session)["query_classes"] == 1
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        assert proof.require_eliminated_source(f"s{origins['conditional']}")["initialization"] == "vendor-confirmed"
    assert f"optimized-away:s{origins['conditional']}" in (out/"local_path_timing-resolved.tsv").read_text()
    rows = list(csv.DictReader((out/"timing-exports.tsv").open(), delimiter="\t"))
    assert len(rows) == 1
    assert bytes.fromhex(rows[0]["output_hex"]).decode() == str(out/"local_path_timing.tsv")


def test_unrequired_conditional_states_remain_unproved(tmp_path):
    session, facts, origins = session_fixture(tmp_path, require_constant=False)
    out = Path(session["output"])
    assert prepare_frontiers(session)["constant_sources"] == 0
    assert confirm_frontiers(session)["confirmed_sources"] == 0
    prepare_timing(session)
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        with pytest.raises(ValidationError):
            proof.require_eliminated_source(f"s{origins['conditional']}")
    assert "optimized-away" not in (out/"local_path_timing-resolved.tsv").read_text()


def test_unassociated_nonconstant_endpoint_fails_before_placement(tmp_path):
    session, _, _ = session_fixture(tmp_path)
    inventory = Path(session["output"])/"mapped_cells.tsv"
    inventory.write_text("\n".join(line for line in inventory.read_text().splitlines() if not line.startswith("p/kept\t"))+"\n")
    with pytest.raises(ValidationError, match="no supported native association"):
        prepare_frontiers(session)


def test_changed_checkpoint_or_native_driver_cannot_promote_constants(tmp_path):
    session, facts, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    prepare_frontiers(session)
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    checkpoint = Path(session["synthesized_checkpoint"])
    checkpoint.write_bytes(b"a different checkpoint")
    with pytest.raises(ValidationError, match="different synthesized checkpoint"):
        confirm_frontiers(session)
    checkpoint.write_bytes(b"checked synthetic native snapshot")
    (out/"constant-frontiers.tsv").write_text(facts.read_text().replace("\tI0\tzero\t", "\tI0\tone\t"))
    with pytest.raises(ValidationError, match="pin bindings"):
        prepare_timing(session)
    assert not (out/"optimization-proofs.sqlite").exists()


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_current_snapshot_exports_require_native_stage_and_do_not_reopen_checkpoint(tmp_path):
    helper = ROOT/"scripts/vivado/timing_contract.tcl"
    source = f"source {_tcl_word(helper)}\n"+r'''
proc current_design {args} {return current}
proc open_checkpoint {path} {global opened;set opened $path}
if {![catch {emuflow_open_export_design @current-design routed}]} {error "unchecked snapshot accepted"}
set ::emuflow_native_export_stage synthesized
if {![catch {emuflow_open_export_design @current-design routed}]} {error "wrong stage accepted"}
emuflow_open_export_design @current-design synthesized
if {[info exists opened]} {error "current snapshot reopened a checkpoint"}
set ::emuflow_native_export_stage routed
emuflow_open_export_design @current-design routed
emuflow_open_export_design explicit.dcp routed
if {![string match */explicit.dcp $opened]} {error "legacy explicit checkpoint was not opened"}
puts pass
'''
    result = subprocess.run([shutil.which("tclsh")], input=source, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.strip() == "pass"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_native_child_contract_survives_tcl_characters_and_isolates_vendor_python(tmp_path):
    out = tmp_path/'native $path[0] "quoted"';out.mkdir()
    runtime = {"fabric_clock": {"period_ns": 4}, "virtual_dut_clocks": {"clk": {"target_period_ns": 20, "nominal_period_ns": 200}},
               "timing_model": {"fabric_clock_port": "fabric_clk", "dut_clock_ports": ["clk"]}}
    config = prepare_manifest(out, fpga="fpga0", part="part", mapped_index=tmp_path/"mapped.sqlite", functions=tmp_path/"functions.sqlite",
        mapped_sha256="a"*64, queries={}, runtime=runtime, ports={"clk", "fabric_clk"})
    code = f"source {_tcl_word(config)}\nputs [lindex $emuflow_child_command 0]\nputs $emuflow_session_out\nputs $emuflow_start_checkpoint\n"
    result = subprocess.run([shutil.which("tclsh")], input=code, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.splitlines() == ["/usr/bin/env", str(out), ""]


def test_continuation_rejects_changed_runtime_source_or_checkpoint(tmp_path):
    mapped, functions, _, _, _, digest, _ = conditional_model(tmp_path)
    old = tmp_path/"old";old.mkdir();new = tmp_path/"new";new.mkdir()
    checkpoint = old/"synthesized.dcp";checkpoint.write_bytes(b"snapshot")
    for path in (old/"runtime.xdc", new/"runtime.xdc"):
        path.write_text("original runtime")
    verilog = tmp_path/"dut.v"
    report = dict(schema="emuflow.vivado-netlist-report/v1", status="pass", provider="test", optimization_policy="policy", top="dut", emitted_instances=3,
        output_sha256=file_sha256(verilog), mapped_index=dict(path=str(mapped), sha256=file_sha256(mapped)),
        source_functions=dict(path=str(functions), sha256=file_sha256(functions)))
    source_report = tmp_path/"source-report.json";source_report.write_text(json.dumps(report))
    def validate():
        return validate_synthesized_continuation(checkpoint,file_sha256(checkpoint),source_report,report,verilog,new)
    assert validate()["status"] == "pass"
    (new/"runtime.xdc").write_text("different runtime")
    with pytest.raises(ValidationError, match="runtime/fixed-I/O"):
        validate()
    (new/"runtime.xdc").write_text("original runtime")
    changed = {**report, "top": "other"};source_report.write_text(json.dumps(changed))
    with pytest.raises(ValidationError, match="source report"):
        validate()
    source_report.write_text(json.dumps(report))
    with pytest.raises(ValidationError, match="checkpoint digest"):
        validate_synthesized_continuation(checkpoint,"0"*64,source_report,report,verilog,new)


@pytest.mark.skipif(TCL86 is None, reason="native stage bridge requires Tcl 8.6")
@pytest.mark.parametrize("resume", [False, True])
def test_native_stage_bridge_runs_real_sqlite_children_without_placed_or_routed_checkpoints(tmp_path, resume):
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    out = Path(session["output"])
    template = tmp_path/"observed.tsv";shutil.copyfile(out/"mapped_cells.tsv", template)
    for file in (out/"mapped_cells.tsv", out/"routed_mapped_cells.tsv"):
        file.unlink()
    prior = tmp_path/"prior.dcp"
    prior.write_bytes((out/"synthesized.dcp").read_bytes())
    (out/"synthesized.dcp").unlink()
    (out/".tmp").mkdir()
    runtime = {"fabric_clock": {"period_ns": 4}, "virtual_dut_clocks": {"clk": {"target_period_ns": 20, "nominal_period_ns": 200}},
               "timing_model": {"fabric_clock_port": "fabric_clk", "dut_clock_ports": ["clk"]}}
    config = prepare_manifest(out, fpga="fpga0", part="xcpart", mapped_index=Path(session["mapped_index"]),
        functions=Path(session["functions"]), mapped_sha256=session["mapped_index_sha256"], queries={}, runtime=runtime,
        ports={"clk", "fabric_clk"}, synthesized_checkpoint=prior if resume else None,
        synthesized_checkpoint_sha256=file_sha256(prior) if resume else None)
    native = ROOT/"scripts/vivado/implement_indexed_partition.tcl"
    arguments = ["xcpart", "netlist.v", "dut", "runtime.xdc", str(out), "3", "", "", "", "", "", "Default", "Default", "1", "2", str(config)]
    script = "set argv [list "+" ".join(_tcl_word(v) for v in arguments)+"]\nset argc [llength $argv]\n"
    script += f"set template {_tcl_word(template)}\nset trace_path {_tcl_word(out/'trace.txt')}\n"
    script += r'''
set trace {}
rename source original_source
proc source {path} {
  if {[file tail $path] eq "source_inventory.tcl"} {
    proc emuflow_inventory {path prefix {placed 0}} {file copy -force $::template $path;return 2}
  } elseif {[file tail $path] eq "resource_inventory.tcl"} {
    proc emuflow_resource_inventory {} {return {lut 1 ff 1 carry8 0 dsp48 0 uram288 0 ramb18_cells 0 ramb36_cells 0 routed_cells 2 physical_cells 2 infrastructure_cells 0 optimization_cells 0}}
  } else {uplevel 1 [list original_source $path]}
}
rename exit process_exit
proc exit {status} {set f [open $::trace_path w];puts $f $::trace;close $f;process_exit $status}
proc set_param {args} {}
proc get_parts {args} {return xcpart}
proc current_design {args} {return dut}
proc create_project {args} {}
proc read_verilog {args} {lappend ::trace read_verilog}
proc read_xdc {args} {}
proc synth_design {args} {lappend ::trace synth}
proc open_checkpoint {path} {lappend ::trace open}
proc get_pblocks {args} {return {}}
proc get_cells {args} {return {}}
proc get_ports {args} {return [lindex $args end]}
proc get_clocks {args} {
 if {[lsearch -exact $args -of_objects]>=0} {
  return [expr {[lindex $args end] eq "fabric_clk" ? "emuflow_tdm_clk" : "emuflow_dut_clk_0"}]
 }
 if {[llength $args]==1} {return {emuflow_tdm_clk emuflow_dut_clk_0}}
 set pattern [lindex $args end]
 return [lsearch -all -inline -glob {emuflow_tdm_clk emuflow_dut_clk_0} $pattern]
}
proc get_property {property object} {
 switch $property {
  PART {return xcpart}
  NAME {return [expr {$object eq "dut" ? "checkpoint_synthesized" : $object}]}
  TOP {return dut}
  PERIOD {return [expr {$object eq "emuflow_tdm_clk" ? 4.0 : 20.0}]}
  SLACK {return 0.5}
  DATAPATH_DELAY {return 2.5}
 }
 error "unexpected mock property $property"
}
proc write_checkpoint {args} {set p [lindex $args end];lappend ::trace [file tail $p];set f [open $p w];puts $f snapshot;close $f}
proc opt_design {} {lappend ::trace opt}
proc place_design {args} {lappend ::trace place}
proc phys_opt_design {} {lappend ::trace phys_opt}
proc route_design {args} {lappend ::trace route}
foreach cmd {report_route_status report_drc report_timing_summary report_utilization report_design_analysis} {
 proc $cmd {args} {set i [lsearch -exact $args -file];set f [open [lindex $args [expr {$i+1}]] w];puts $f report;close $f}
}
proc get_drc_violations {args} {return {}}
proc get_nets {args} {return {}}
proc get_timing_paths {args} {return path}
proc version {args} {return 2024.2}
# The child must use its original Python runtime rather than these vendor vars.
set env(PYTHONHOME) /invalid-vendor-python
set env(PYTHONPATH) /invalid-vendor-package
'''
    script += f"source {_tcl_word(native)}\n"
    completed = subprocess.run([TCL86], input=script, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stdout+completed.stderr
    trace = (out/"trace.txt").read_text().split()
    assert trace[-4:] == ["opt", "place", "phys_opt", "route"]
    assert ("synth" in trace) is (not resume)
    assert ("open" in trace) is resume
    assert "placed.dcp" not in trace and "routed.dcp" not in trace
    assert read_json(out/"routed-query-preparation.json")["status"] == "pass"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_synthesized_module_identity_is_independent_of_checkpoint_container_name():
    helper = ROOT/"scripts/vivado/native_session.tcl"
    script = f"source {_tcl_word(helper)}\n"+r'''
set part xcpart
set top actual_module
proc current_design {args} {return checkpoint_renamed_snapshot}
proc get_property {property object} {
 if {$property eq "PART"} {return $::part}
 if {$property eq "TOP"} {return $::top}
 if {$property eq "NAME"} {return checkpoint_renamed_snapshot}
 error "unexpected property $property"
}
emuflow_validate_synthesized_identity xcpart actual_module
set top wrong_module
if {![catch {emuflow_validate_synthesized_identity xcpart actual_module}]} {error "wrong module accepted"}
set top actual_module
set part wrong_part
if {![catch {emuflow_validate_synthesized_identity xcpart actual_module}]} {error "wrong part accepted"}
puts pass
'''
    result = subprocess.run([shutil.which("tclsh")],input=script,text=True,capture_output=True,check=True)
    assert not result.stderr
    assert result.stdout.strip() == "pass"


@pytest.mark.parametrize("dependent", [False, True])
def test_postopt_preflight_confirms_states_removed_only_after_synthesis(tmp_path, dependent):
    session, facts, origins = session_fixture(tmp_path, dependent=dependent)
    session["source_preflight_stage"] = "optimized"
    out = Path(session["output"])
    shutil.copyfile(out/"mapped_cells.tsv", out/"optimized_mapped_cells.tsv")
    # These endpoints existed in the synthesis snapshot and disappeared in opt_design.
    with (out/"mapped_cells.tsv").open("a") as stream:
        stream.write(f"p/conditional\tFDRE\ts{origins['conditional']}\t\t\t\t\t\t\n")
    result = prepare_frontiers(session)
    assert result["constant_sources"] == (2 if dependent else 1)
    assert read_json(out/"optimized-source-preflight.json")["source_stage"] == "optimized"
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    confirm_frontiers(session)
    assert read_json(out/"checked-constant-frontiers.json")["source_stage"] == "optimized"
    prepare_timing(session)
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        assert proof.require_eliminated_source(f"s{origins['conditional']}")["initialization"] == "vendor-confirmed"
    assert not (out/"optimized_mapped_cells.tsv").exists()


def test_postopt_preflight_rejects_a_facts_stage_mismatch(tmp_path):
    session, facts, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    prepare_frontiers(session)
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    session["source_preflight_stage"] = "optimized"
    with pytest.raises(ValidationError, match="different preplacement stage"):
        confirm_frontiers(session)
