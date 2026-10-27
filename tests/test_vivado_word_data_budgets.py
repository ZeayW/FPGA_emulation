"""Separate scheduled DUT data from fixed-rate word controls during implementation."""
import csv
from pathlib import Path
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json, write_json
from emuflow.vivado_session import _tcl_word, load_session, prepare_manifest, prepare_routed_timing_export
from tests.test_vivado_session import ROOT, TCL86, session_fixture


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
@pytest.mark.parametrize("rx_sources", [[], ["s7"]])
def test_data_budgets_leave_controls_hold_and_dut_clock_relationships_intact(tmp_path, rx_sources):
    origins = tmp_path/"origins.tsv"
    origins.write_text("source_origin\n"+"".join(origin+"\n" for origin in rx_sources))
    program = f"source {_tcl_word(ROOT/'scripts/vivado/word_data_budgets.tcl')}\n"
    program += f"source {_tcl_word(ROOT/'scripts/vivado/timing_contract.tcl')}\n"
    program += r'''
set constraints {}
proc get_clocks {args} {
 if {$args eq {-quiet}} {return {emuflow_dut_clk_0 dut_generated emuflow_tdm_clk phy}}
 return [lindex $args end]
}
proc get_property {args} {
 lassign [lrange $args end-1 end] key object
 switch $key {
  IS_GENERATED {return [expr {$object in {dut_generated phy}}]}
  MASTER_CLOCK {return [expr {$object eq "phy" ? "emuflow_tdm_clk" : "emuflow_dut_clk_0"}]}
  PERIOD {return [expr {$object in {emuflow_dut_clk_0 dut_generated} ? 100 : 5}]}
  NAME {return $object}
  EMUFLOW_SOURCE {return [expr {$object in {rx rx_replica} ? "s7" : "s999"}]}
 }
 error "unexpected property $key"
}
proc all_registers {args} {
 switch [lindex $args 1] {
  emuflow_tdm_clk {return {rx/C rx_replica/C control/C}}
  phy {return {rx_replica/C phy_control/C}}
 }
 error "DUT registers must not be classified as RX data"
}
proc get_cells {args} {return [file dirname [lindex $args end]]}
proc set_max_delay {args} {lappend ::constraints $args}
proc set_min_delay {args} {error "hold timing was relaxed"}
proc set_false_path {args} {error "data path was hidden"}
proc set_clock_groups {args} {error "data clock relationship was hidden"}
proc create_clock {args} {error "clock period was changed"}
'''
    program += f"puts [emuflow_word_data_budgets {_tcl_word(origins)}]\n"
    program += "foreach row $constraints {puts $row}\nputs reference\nset constraints {}\nemuflow_use_clock_aware_reference_constraints\nforeach row $constraints {puts $row}\n"
    done = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    assert not done.stderr
    rows = done.stdout.splitlines()
    assert rows[0] == ("100 2" if rx_sources else "100 0")
    delimiter = rows.index("reference")
    applied = rows[1:delimiter]
    assert len(applied) == (2 if rx_sources else 1)
    assert applied[-1] == "100 -from {emuflow_dut_clk_0 dut_generated} -to {emuflow_tdm_clk phy}"
    if rx_sources:
        assert applied[0] == "100 -from {rx/C rx_replica/C} -to {emuflow_dut_clk_0 dut_generated emuflow_tdm_clk phy}"
    reference = rows[delimiter+1:]
    assert len(reference) == len(applied)+1
    assert all(row.endswith("1.0") for row in reference)
    assert all("control/C" not in row and "phy_control/C" not in row for row in applied+reference)


def test_rx_role_catalog_survives_independent_local_export_and_detects_tampering(tmp_path):
    session, _, _ = session_fixture(tmp_path)
    output = Path(session["output"])
    boundary = tmp_path/"boundary.tsv"
    with boundary.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["endpoint_hex", "kind", "external_port_hex", "bit", "logical_net_hex", "boundary_cell_hex"])
        for endpoint in ["rx0", "rx1"]:
            writer.writerow([endpoint.encode().hex(), "rx", b"rx_port".hex(), "0", b"data".hex(), b"kept".hex()])
        writer.writerow([b"tx".hex(), "tx", b"tx_port".hex(), "0", b"data".hex(), b"not-an-rx-register".hex()])
    (output/"native-word-clocks.tsv").write_text("clock_hex\tport_hex\tperiod_ns\n")
    runtime = {"fabric_clock":{"period_ns":5}, "virtual_dut_clocks":{"clk":{"target_period_ns":100}},
        "timing_model":{"fabric_clock_port":"fabric_clk", "dut_clock_ports":["clk"]}}
    config = prepare_manifest(output, fpga="fpga0", part="part", mapped_index=Path(session["mapped_index"]),
        functions=Path(session["functions"]), mapped_sha256=session["mapped_index_sha256"],
        queries={"boundary_timing":boundary, "local_path_timing":Path(session["queries"]["local_path_timing"])},
        runtime=runtime, ports={"clk", "fabric_clk"}, word_phy_directory=output, source_hierarchy_prefix="p")
    original = config.with_suffix(".json")
    role = load_session(original)["word_data_budget"]
    assert role["rx_queries"] == 2 and role["source_origins"] == 1
    assert role["boundary_query_sha256"] == file_sha256(boundary)
    assert len(Path(role["path"]).read_text().splitlines()) == 2
    (output/"routed.dcp").write_bytes(b"independent retained implementation")
    recovered = prepare_routed_timing_export(original, tmp_path/"local", query_kinds=["local_path_timing"])
    assert load_session(recovered.with_suffix(".json"))["word_data_budget"] == role
    assert 'set emuflow_word_data_source_origins ' in recovered.read_text()
    Path(role["path"]).write_text("source_origin\ns999999\n")
    with pytest.raises(ValidationError, match="data budget source contract"):
        load_session(original)
