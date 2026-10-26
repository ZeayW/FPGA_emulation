"""Record/byte migration gates, not physical or NVDLA qualification."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from emuflow.io import file_sha256
from emuflow.errors import ValidationError
from emuflow.native_protocol import CheckedRequestWriter
from emuflow.native_tools import native_file_generation, resolve_native_executable
from emuflow.vivado_indexed_netlist import emit_indexed_vivado_verilog
from emuflow.xilinx_netlist import emit_xilinx_mapped_index
from tests.test_vivado_indexed_physical import fixture
from tests.vivado_verilog_reference import emit_indexed_vivado_verilog_reference


def request_for(mapped, target, count=None):
    if count is None:
        with sqlite3.connect(mapped) as db:
            count = db.execute("SELECT count(*) FROM cells").fetchone()[0]
    writer = CheckedRequestWriter(target)
    writer.put("QI", 0x31585656464d5545, 1)
    writer.put("3Q4q", *native_file_generation(mapped))
    writer.text(file_sha256(mapped))
    writer.put("Q", count)
    writer.finish()
    return target


def run_native(mapped, request, rtl, functions, summary):
    return subprocess.run([resolve_native_executable("emuflow-native"), "phase7", "export-vivado",
                           str(mapped), str(request), str(rtl), str(functions), str(summary)],
                          capture_output=True, text=True)


def signatures(path):
    with sqlite3.connect(path) as db:
        return list(db.execute("SELECT seq,signature FROM signatures ORDER BY seq"))


def test_production_export_has_no_python_cell_or_pin_population(tmp_path):
    ir = fixture(tmp_path)
    expected = emit_indexed_vivado_verilog_reference(ir, tmp_path / "reference.v")
    with patch("emuflow.xilinx_netlist._IndexedCells.items", side_effect=AssertionError("Python cell export")):
        actual = emit_indexed_vivado_verilog(ir, tmp_path / "native.v")
    assert (tmp_path / "native.v").read_bytes() == (tmp_path / "reference.v").read_bytes()
    assert signatures(Path(actual["source_functions"]["path"])) == signatures(Path(expected["source_functions"]["path"]))
    assert actual["output_sha256"] == file_sha256(tmp_path / "native.v")
    assert actual["implementation_id"] == "native-vivado-export-v1"


@pytest.mark.parametrize("parameters", [
    {}, {"INIT": "X"}, {"INIT": "xZ"}, {"INIT": False},
    {"INIT": 0, "LABEL": "中文\\quote\"\n", "BIG": 2**130 + 7},
    {"REAL": 1.23456789012345, "SMALL": 1e-9, "NEG_ZERO": -0.0, "UNSET": None},
])
def test_parameter_bytes_and_function_signatures_match_reference(tmp_path, parameters):
    ir = fixture(tmp_path)
    mapped = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(ir, mapped)
    with sqlite3.connect(mapped) as db:
        rows = list(db.execute("SELECT id,record_json FROM cells"))
        for name, text in rows:
            cell = json.loads(text)
            cell["parameters"] = parameters
            db.execute("UPDATE cells SET record_json=? WHERE id=?", (json.dumps(cell, ensure_ascii=False), name))
    expected = emit_indexed_vivado_verilog_reference(ir, tmp_path / "reference.v", mapped_index=mapped)
    request = request_for(mapped, tmp_path / "request.bin")
    result = run_native(mapped, request, tmp_path / "native.v", tmp_path / "native.sqlite", tmp_path / "summary.json")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "native.v").read_bytes() == (tmp_path / "reference.v").read_bytes()
    assert signatures(tmp_path / "native.sqlite") == signatures(Path(expected["source_functions"]["path"]))


@pytest.mark.parametrize("mutation", ["orphan", "pin-owner", "missing-bit", "type", "count", "crc", "generation"])
def test_invalid_input_preserves_previous_outputs(tmp_path, mutation):
    ir = fixture(tmp_path)
    mapped = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(ir, mapped)
    if mutation in {"orphan", "pin-owner", "missing-bit", "type"}:
        with sqlite3.connect(mapped) as db:
            if mutation == "orphan":
                db.execute("UPDATE cell_pins SET cell_seq=9000 WHERE cell_seq=(SELECT max(seq) FROM cells)")
            elif mutation == "pin-owner":
                db.execute("UPDATE cell_pins SET cell_id='unknown' WHERE cell_seq=(SELECT min(seq) FROM cells)")
            elif mutation == "missing-bit":
                db.execute("UPDATE cell_pins SET port_width=2 WHERE port='C'")
            else:
                db.execute("UPDATE cells SET type='LUT1'")
    request = request_for(mapped, tmp_path / "request.bin", count=9000 if mutation == "count" else None)
    if mutation == "crc":
        data = bytearray(request.read_bytes());data[-1] ^= 1;request.write_bytes(data)
    if mutation == "generation":
        os.utime(mapped, ns=(mapped.stat().st_atime_ns, mapped.stat().st_mtime_ns + 1000000))
    outputs = [tmp_path / name for name in ("out.v", "functions.sqlite", "summary.json")]
    # The auxiliary old SQLite must be valid for the common output contract.
    run_ok = run_native(mapped, request, *outputs)
    assert run_ok.returncode != 0
    for output in outputs:
        assert not output.exists()
    for output in outputs:
        output.write_bytes(b"previous output")
    result = run_native(mapped, request, *outputs)
    assert result.returncode != 0
    assert all(output.read_bytes() == b"previous output" for output in outputs)


def test_repeated_typed_cells_are_consumed_in_one_native_operation(tmp_path):
    ir = fixture(tmp_path)
    mapped = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(ir, mapped)
    count = 20000
    with sqlite3.connect(mapped) as db:
        for i in range(count):
            seq, net, name = 100 + i, 100 + i, f"重复_cell_{i}"
            record = json.dumps({"type": "LUT1", "parameters": {"INIT": "10"}, "attributes": {}}, ensure_ascii=False)
            db.execute("INSERT INTO cells VALUES(?,?,?,?)", (name, seq, "LUT1", record))
            db.execute("INSERT INTO netnames VALUES(?,?,?)", (f"net{i}", net, "{}"))
            db.execute("INSERT INTO cell_pins VALUES(?,?,?,?,?,?,?,?,?)", (seq, name, "I0", 0, 1, 0, 1, None, "1"))
            db.execute("INSERT INTO cell_pins VALUES(?,?,?,?,?,?,?,?,?)", (seq, name, "O", 0, 1, 1, 0, net + 2, None))
    request = request_for(mapped, tmp_path / "request.bin")
    result = run_native(mapped, request, tmp_path / "out.v", tmp_path / "functions.sqlite", tmp_path / "summary.json")
    assert result.returncode == 0, result.stderr
    assert len(signatures(tmp_path / "functions.sqlite")) == count + 2
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["cells"] == count + 2
    assert summary["rtl_sha256"] == file_sha256(tmp_path / "out.v")


@pytest.mark.parametrize("previous", [False, True])
def test_late_group_publication_failure_restores_both_outputs(tmp_path, previous):
    from emuflow import store_publication
    from emuflow.ir import EmuIR

    ir = fixture(tmp_path)
    rtl, functions = tmp_path / "out.v", tmp_path / "out.functions.sqlite"
    old = None
    if previous:
        emit_indexed_vivado_verilog(ir, rtl)
        old = (rtl.read_bytes(), functions.read_bytes())
        value = EmuIR.load(ir).to_dict()
        value["instances"][0]["parameters"]["INIT"] = "1"
        EmuIR(value).save(ir)
    publish = store_publication.publish_sqlite_store

    def late_failure(temporary, destination, schema, **kwargs):
        result = publish(temporary, destination, schema, **kwargs)
        if Path(destination) == functions:
            raise OSError("late publication failure")
        return result

    with patch.object(store_publication, "publish_sqlite_store", side_effect=late_failure):
        with pytest.raises(OSError, match="late publication failure"):
            emit_indexed_vivado_verilog(ir, rtl)
    if previous:
        assert (rtl.read_bytes(), functions.read_bytes()) == old
    else:
        assert not rtl.exists() and not functions.exists()
    assert not list(tmp_path.glob(".derived-recovery.*"))
    assert not list(tmp_path.glob(".vivado-export.*"))


@pytest.mark.parametrize("role", [0, 1, 2])
def test_native_outputs_cannot_alias_a_source(tmp_path, role):
    ir = fixture(tmp_path)
    mapped = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(ir, mapped)
    request = request_for(mapped, tmp_path / "request.bin")
    before = file_sha256(mapped)
    paths = [tmp_path / "out.v", tmp_path / "functions.sqlite", tmp_path / "summary.json"]
    os.link(mapped, paths[role])
    result = run_native(mapped, request, *paths)
    assert result.returncode != 0
    assert file_sha256(mapped) == before


def test_host_output_aliases_are_rejected_before_mapping(tmp_path):
    ir = fixture(tmp_path)
    before = file_sha256(ir)
    with pytest.raises(ValidationError, match="aliases"):
        emit_indexed_vivado_verilog(ir, ir)
    assert file_sha256(ir) == before
    with pytest.raises(ValidationError, match="alias"):
        emit_indexed_vivado_verilog(ir, tmp_path / "out.v", tmp_path / "out.v")
    assert not (tmp_path / "out.v").exists()


def test_compile_requires_export_capability_before_frontend(tmp_path):
    from emuflow.errors import EmuFlowError
    from emuflow.multi_fpga_flow import run_multi_fpga_flow

    def capabilities(schema):
        if schema == "vivado-export-request-v1":
            raise EmuFlowError("missing native primitive exporter")

    with patch("emuflow.multi_fpga_flow._validate_mapping_timing_contract"), patch(
        "emuflow.native_tools.require_native_schema", side_effect=capabilities
    ), patch(
        "emuflow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend started")
    ), pytest.raises(EmuFlowError, match="missing native primitive exporter"):
        run_multi_fpga_flow(sources=[tmp_path / "missing.v"], top="dut", clocks=["clk"],
            clock_periods={"clk": 10.0}, physical=True, physical_backend="vivado",
            output_dir=tmp_path / "flow")
    assert not (tmp_path / "flow").exists()
