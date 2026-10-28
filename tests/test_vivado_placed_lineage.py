from tests.compile_adapter_fixture import preflight as run_preflight
"""Native lineage/placement contract tests; not a routed design qualification."""
import csv
import hashlib
import sqlite3
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.physical.vivado.vivado_lineage import LINEAGE_SCHEMA, PLACEMENT_COORDINATE_SYSTEM, import_cell_lineage


@pytest.fixture
def inputs(tmp_path):
    from tests.test_vivado_lineage import VivadoLineageTests
    return VivadoLineageTests().prepare(tmp_path)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"no-final-newline"])
def test_native_preserves_exact_inventory_hash_and_source_scoped_coordinates(inputs, tmp_path, newline):
    report, mapped, functions, inventory = inputs
    raw = inventory.read_bytes()
    raw = raw.replace(b"\n", newline) if newline != b"no-final-newline" else raw.rstrip(b"\n")
    inventory.write_bytes(raw)
    output = tmp_path / "lineage.sqlite"
    result = import_cell_lineage(mapped, functions, {"routed": inventory}, output,
                                mapped_sha256=report["mapped_index"]["sha256"])
    assert result["schema"] == LINEAGE_SCHEMA
    assert result["coordinate_system"] == PLACEMENT_COORDINATE_SYSTEM
    assert result["stages"]["routed"]["placed_objects"] == 3
    assert result["stages"]["routed"]["inventory_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["sha256"] == file_sha256(output)
    assert result["bytes"] == output.stat().st_size
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0x454D564C
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT value FROM metadata WHERE key='mapped_index_sha256'").fetchone()[0] == report["mapped_index"]["sha256"]
        assert db.execute("SELECT tile_column,tile_row FROM objects JOIN sites ON sites.seq=objects.site_seq ORDER BY name").fetchall() == [(1, 0), (0, 0), (2, 0)]


@pytest.mark.parametrize("mutation", ["negative", "partial", "unplaced", "duplicate-bel", "level", "macro-with-placement", "same-site-mismatch", "old-header"])
def test_failed_import_preserves_previous_owner_and_retires_staging(inputs, tmp_path, mutation):
    report, mapped, functions, inventory = inputs
    output = tmp_path / "lineage.sqlite"
    import_cell_lineage(mapped, functions, {"routed": inventory}, output,
                        mapped_sha256=report["mapped_index"]["sha256"])
    previous = file_sha256(output)
    with inventory.open() as stream: rows = list(csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
    if mutation == "negative": rows[1][6] = "-1"
    elif mutation == "partial": rows[1][5] = ""
    elif mutation == "unplaced": rows[1][4:8] = [""] * 4
    elif mutation == "duplicate-bel": rows[2][4:8] = rows[1][4:8]
    elif mutation == "level": rows[1][8] = "guess"
    elif mutation == "macro-with-placement": rows[1][8] = "MACRO"
    elif mutation == "same-site-mismatch": rows[2][4] = rows[1][4]
    else: rows = [row[:4] for row in rows]
    with inventory.open("w") as stream:
        csv.writer(stream, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE).writerows(rows)
    with pytest.raises(ValidationError):
        import_cell_lineage(mapped, functions, {"routed": inventory}, output,
                            mapped_sha256=report["mapped_index"]["sha256"])
    assert file_sha256(output) == previous
    assert not list(tmp_path.glob("lineage.sqlite.stage.*"))


def test_macro_wrapper_is_observed_without_consuming_a_second_bel(inputs, tmp_path):
    report, mapped, functions, inventory = inputs
    with inventory.open("a") as stream:
        csv.writer(stream, delimiter="\t", lineterminator="\n").writerow(
            ["partition/wrapper", "FDRE", "s0", "", "", "", "", "", "MACRO"])
    result = import_cell_lineage(mapped, functions, {"routed": inventory}, tmp_path / "lineage.sqlite",
                                mapped_sha256=report["mapped_index"]["sha256"])
    assert result["stages"]["routed"]["observed_objects"] == 4
    assert result["stages"]["routed"]["placed_objects"] == 3
    assert result["stages"]["routed"]["unplaced_objects"] == 1


@pytest.mark.skipif(not shutil.which("tclsh"), reason="requires Tcl interpreter")
def test_tool_export_uses_native_tile_coordinates_and_retains_macro_identity(tmp_path):
    helper = Path(__file__).resolve().parents[1] / "scripts/vivado/source_inventory.tcl"
    output = tmp_path / "inventory.tsv"
    script = r'''
if {[catch {binary encode hex a}]} {
  rename binary native_binary
  proc binary {operation format value} {native_binary scan $value H* result; return $result}
}
proc get_cells {args} {return {leaf wrapper}}
proc get_sites {args} {return {SLICE_X9Y8}}
proc get_tiles {args} {return {native_tile}}
proc get_property {args} {
  set key [lindex $args end-1];set object [lindex $args end]
  switch $key {
    NAME {return $object}
    EMUFLOW_SOURCE {return s0}
    REF_NAME {return FDRE}
    PRIMITIVE_LEVEL {if {$object eq "wrapper"} {return MACRO};return LEAF}
    BEL {return AFF}
    COLUMN {return 7}
    ROW {return 11}
    default {return ""}
  }
}
'''
    wrapper = tmp_path / "run.tcl"
    wrapper.write_text(script + f"\nsource {{{helper}}}\nemuflow_inventory {{{output}}} {{}} 1\n")
    completed = subprocess.run([shutil.which("tclsh"), str(wrapper)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    with output.open() as stream: rows = list(csv.DictReader(stream, delimiter="\t"))
    assert (rows[0]["site"], rows[0]["tile_column"], rows[0]["tile_row"]) == ("SLICE_X9Y8", "7", "11")
    assert rows[1]["primitive_level"] == "MACRO" and rows[1]["site"] == ""


def test_native_registry_preflight_accepts_current_and_rejects_unknown_schema():
    from emuflow.common.native_tools import require_native_schema
    from emuflow.common.errors import EmuFlowError
    require_native_schema("vivado-lineage-store-v2")
    with pytest.raises(EmuFlowError, match="required schema"):
        require_native_schema("vivado-lineage-store-unknown")


def test_compile_rejects_incomplete_native_installation_before_frontend(tmp_path):
    from unittest.mock import patch
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    # Other preflight tests qualify the upstream timing contract independently.
    with patch("emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract"), patch(
        "emuflow.common.native_tools.require_native_schema", side_effect=EmuFlowError("missing lineage capability")
    ), patch(
        "emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend started")
    ), pytest.raises(EmuFlowError, match="missing lineage capability"):
        run_preflight(sources=[tmp_path / "missing.v"], top="dut", clocks=["clk"],
            clock_periods={"clk": 10.0}, physical=True, physical_backend="vivado",
            output_dir=tmp_path / "flow")
    assert not (tmp_path / "flow").exists()


def test_packed_cells_share_one_integer_site_owner(inputs, tmp_path):
    report, mapped, functions, inventory = inputs
    with inventory.open() as stream: rows = list(csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
    rows[2][4:8] = rows[1][4:8]
    rows[2][5] = "BFF"
    with inventory.open("w") as stream:
        csv.writer(stream, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE).writerows(rows)
    output = tmp_path / "lineage.sqlite"
    import_cell_lineage(mapped, functions, {"routed": inventory}, output,
                        mapped_sha256=report["mapped_index"]["sha256"])
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT count(*) FROM sites").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM objects").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM objects WHERE site_seq=(SELECT seq FROM sites WHERE site=?)",
                          (rows[1][4],)).fetchone()[0] == 2
