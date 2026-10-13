import json
import sqlite3
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.emuir_compact import repack_emuir
from emuflow.emuir_storage import _write_emuir_sqlite_v1
from emuflow.indexed_timing import IndexedTimingView
from emuflow.ir import EmuIR
from emuflow.opensta import run_opensta_path_database
from emuflow.resources import ResourceVector
from emuflow.sta import sta_object_index, write_emuir_net_map, iter_sta_path_database_paths
from emuflow.verilog import mapped_verilog, write_mapped_verilog
from emuflow.yosys import import_yosys_json

ROOT = Path(__file__).resolve().parents[1]


def test_compact_stream_preserves_exact_timing_netlist_and_endpoint_identity(tmp_path):
    ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
    source, compact = tmp_path / "old.emuir.sqlite", tmp_path / "new.emuir.sqlite"
    _write_emuir_sqlite_v1(source, ir.value)
    with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole-graph loading forbidden")):
        repack_emuir(source, compact)
    assert EmuIR.load(source).value == EmuIR.load(compact).value
    view = IndexedTimingView(compact)
    try:
        out = tmp_path / "timing.v"
        with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole-graph loading forbidden")):
            write_mapped_verilog(view, out, timing_only=True)
            write_emuir_net_map(compact, tmp_path / "stream.tsv", _ir=view)
        assert out.read_text() == mapped_verilog(ir, timing_only=True)
        write_emuir_net_map(source, tmp_path / "old.tsv", _ir=ir)
        assert (tmp_path / "stream.tsv").read_bytes() == (tmp_path / "old.tsv").read_bytes()
        for name, endpoint in sta_object_index(ir).items():
            assert view.object_index[name] == endpoint
    finally:
        view.close()


def test_compact_stream_uses_real_provider_protocol_without_ir_reload(tmp_path):
    ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
    path = tmp_path / "design.emuir.sqlite"
    ir.save(path)
    out = tmp_path / "paths.json"
    with patch("emuflow.opensta.EmuIR.load", side_effect=AssertionError("whole-graph loading forbidden")):
        report = run_opensta_path_database(
            path, out, clocks={"clk": 10},
            executable=str(ROOT / "tests/fixtures/fake_opensta_paths.py"), _stream_output=True,
        )
    assert report["status"] == "pass"
    records = list(iter_sta_path_database_paths(out, json.loads(out.read_text())))
    assert records
    # Compare the actual path population, not only summary scalars.
    old = tmp_path / "old.emuir.sqlite"
    _write_emuir_sqlite_v1(old, ir.value)
    old_out = tmp_path / "old-paths.json"
    old_report = run_opensta_path_database(
        old, old_out, clocks={"clk": 10},
        executable=str(ROOT / "tests/fixtures/fake_opensta_paths.py"), _stream_output=True,
    )
    assert records == list(iter_sta_path_database_paths(old_out, json.loads(old_out.read_text())))
    assert report["path_qor"] == old_report["path_qor"]


def test_repeated_resources_and_long_endpoint_owners_are_not_stored_per_pin(tmp_path):
    prefix = "dut/partition/subsystem/compute/processing_array/" * 4
    value = {"schema": "emuflow.emuir/v1", "design": {"name": "scale", "top": "scale", "source_format": "test"},
             "ports": [], "instances": [], "nets": [], "clocks": [], "warnings": []}
    for index in range(6000):
        name = prefix + str(index)
        value["instances"].append({"id": name, "name": name, "type": "LUT1",
            "resources": ResourceVector(lut=1).to_dict(), "parameters": {"INIT": "10"},
            "attributes": {"src": "test.v:10"}, "constant_connections": []})
        value["nets"].append({"id": "n" + str(index), "cut_class": "combinational",
            "aliases": [], "drivers": [{"instance": name, "port": "O", "bit": 0}],
            "sinks": [{"instance": name, "port": "I0", "bit": 0}]})
    old, new = tmp_path / "old.emuir.sqlite", tmp_path / "new.emuir.sqlite"
    _write_emuir_sqlite_v1(old, value)
    EmuIR(value).save(new)
    assert new.stat().st_size < old.stat().st_size * 0.5
    assert EmuIR.load(old).value == EmuIR.load(new).value
    view = IndexedTimingView(new)
    try:
        tracemalloc.start()
        write_mapped_verilog(view, tmp_path / "timing.v", timing_only=True)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 4 * 1024 * 1024
    finally:
        view.close()


@pytest.mark.parametrize("statement,pattern", [
    ("UPDATE endpoint_records SET role=2 WHERE role=0", "endpoint role"),
    ("UPDATE endpoint_records SET instance_seq=999999", "orphaned endpoint"),
    ("DELETE FROM payloads WHERE seq=(SELECT extras_seq FROM net_records LIMIT 1)", "invalid canonical JSON"),
])
def test_compact_corruption_is_not_silently_dropped(tmp_path, statement, pattern):
    ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
    path = tmp_path / "design.emuir.sqlite"
    ir.save(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    with pytest.raises(ValueError, match=pattern):
        EmuIR.load(path)
