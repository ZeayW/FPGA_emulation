"""RX data transitions must not masquerade as next-word control checks."""
import csv
from pathlib import Path
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json
from emuflow.vivado_physical import WORD_CONTROL_TIMING_SCOPE, _word_infrastructure_timing
from emuflow.vivado_session import _tcl_word, prepare_timing
from tests.test_vivado_session import ROOT, TCL86, session_fixture


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
@pytest.mark.parametrize("unresolved", [0, 1])
@pytest.mark.parametrize("control_slack", [1.25, -0.2])
@pytest.mark.parametrize("hold_slack", [0.1, -0.3])
def test_word_clock_checks_separate_scheduled_rx_data_from_registered_controls(tmp_path, unresolved, control_slack, hold_slack):
    scope = tmp_path/"rx.tsv"
    scope.write_text("status\trx_queries\tunresolved_rx_queries\tnative_cells\n"+
        f"{'incomplete' if unresolved else 'complete'}\t2\t{unresolved}\t2\ncell_hex\n"+
        "\n".join(name.encode().hex() for name in ["partition/rx", "partition/rx_replica"])+"\n")
    program = f"source {_tcl_word(ROOT/'scripts/vivado/word_native_physical.tcl')}\n"
    program += f"set control_slack {control_slack}\nset hold_slack {hold_slack}\n"
    program += r'''
proc get_clocks {args} {
 if {$args eq {-quiet}} {return {emuflow_dut_clk_0 emuflow_tdm_clk phy_clk riu_clk}}
 return [lindex $args end]
}
proc get_property {key object} {
 switch $key {
  IS_GENERATED {return [expr {$object eq "phy_clk"}]}
  MASTER_CLOCK {return emuflow_tdm_clk}
  PERIOD {return [expr {$object eq "emuflow_dut_clk_0" ? 100 : 5}]}
  NAME {return $object}
  SLACK {return [expr {$object eq "mixed" ? -5.410 : ($object eq "min" ? $::hold_slack : $::control_slack)}]}
 }
 error "unexpected property $key"
}
proc all_registers {args} {
 switch [lindex $args 1] {
  emuflow_tdm_clk {return {partition/rx/C partition/rx_replica/C phase/C}}
  phy_clk {return {phase/C phy/C}}
  riu_clk {return riu/C}
 }
 error "unexpected DUT clock included in control sources"
}
proc get_cells {args} {return [file dirname [lindex $args end]]}
proc get_timing_paths {args} {
 set from [lindex $args [expr {[lsearch -exact $args -from]+1}]]
 if {[lsort $from] ne [lsort {phase/C phy/C riu/C}]} {return mixed}
 return [lindex $args [expr {[lsearch -exact $args -delay_type]+1}]]
}
'''
    program += f"puts [join [emuflow_word_routed_checks {_tcl_word(tmp_path)} {_tcl_word(scope)}] ,]\n"
    result = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.strip() == f"2,{control_slack},2,3,{int(not unresolved)},{hold_slack}"
    metrics = {"native_word_infrastructure_scope":WORD_CONTROL_TIMING_SCOPE,
        "native_word_infrastructure_checks":"2", "native_word_infrastructure_wns_ns":str(control_slack),
        "native_word_infrastructure_whs_ns":str(hold_slack),
        "native_word_rx_clock_pins_excluded":"2", "native_word_control_clock_pins":"3",
        "native_word_rx_scope_complete":str(int(not unresolved))}
    if not unresolved and (control_slack < 0 or hold_slack < 0):
        with pytest.raises(ValidationError, match="registered controls lack passing"):
            _word_infrastructure_timing(metrics, active_banks=1)
    else:
        measured = _word_infrastructure_timing(metrics, active_banks=1)
        assert measured["wns_ns"] == control_slack
        assert measured["whs_ns"] == hold_slack
        assert measured["fixed_clock_contract_met"] is (None if unresolved else True)
        assert measured["rx_scope_complete"] is (not unresolved)


@pytest.mark.parametrize("missing", [False, True])
def test_rx_scope_uses_actual_routed_aliases_and_retains_unresolved_queries(tmp_path, missing):
    session, _, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    inventory = out/"routed_mapped_cells.tsv"
    with inventory.open(newline="") as stream:
        rows = list(csv.reader(stream, delimiter="\t"))
    kept = next(row for row in rows[1:] if row[0] == "p/kept")
    alias = list(kept); alias[0] = "p/kept_replica"
    alias[4] = "SLICE_X999Y0"; alias[6] = "999"; rows.append(alias)
    with inventory.open("w", newline="") as stream:
        csv.writer(stream, delimiter="\t", lineterminator="\n").writerows(rows)
    query = tmp_path/"boundary-query.tsv"
    with query.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["endpoint_hex", "kind", "external_port_hex", "bit", "logical_net_hex", "boundary_cell_hex"])
        writer.writerow([b"rx0".hex(), "rx", b"rx_port".hex(), "0", b"data".hex(), b"kept".hex()])
        if missing:
            writer.writerow([b"rx1".hex(), "rx", b"other_port".hex(), "0", b"other".hex(), b"conditional".hex()])
    session["queries"] = {"boundary_timing":str(query)}
    parents = {path:file_sha256(path) for path in [inventory, query]}
    result = prepare_timing(session)
    assert result["timing_coverage"]["unresolved_queries"] == int(missing)
    preparation = read_json(out/"routed-query-preparation.json")
    scope = preparation["rx_shadow_scope"]
    assert scope["status"] == ("incomplete" if missing else "complete")
    assert scope["requested_queries"] == 1+int(missing)
    assert scope["unresolved_queries"] == int(missing)
    assert scope["native_cells"] == 2
    lines = Path(scope["path"]).read_text().splitlines()
    assert {bytes.fromhex(line).decode() for line in lines[3:]} == {"p/kept", "p/kept_replica"}
    assert scope["sha256"] == file_sha256(Path(scope["path"]))
    assert parents == {path:file_sha256(path) for path in parents}
