from tests.compile_adapter_fixture import preflight as run_preflight
"""Shared-core timing export; fixture gates do not qualify NVDLA."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tracemalloc
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.timing.indexed_timing import IndexedTimingView, _TimingTypes
from emuflow.ir.ir import EmuIR
from emuflow.timing.opensta_export_native import export_opensta_inputs
from emuflow.timing.sta import write_emuir_net_map
from emuflow.frontend.verilog import mapped_verilog, write_mapped_verilog
from emuflow.frontend.yosys import import_yosys_json

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def counter(tmp_path):
    ir = import_yosys_json(ROOT/"examples/yosys/counter.json", top="counter", clocks=["clk"])
    path = tmp_path/"source ?#%é.emuir.sqlite"
    ir.save(path)
    return ir, path


def test_combined_producer_does_not_iterate_python_graph_or_endpoints(tmp_path):
    ir, path = counter(tmp_path)
    expected = mapped_verilog(ir, timing_only=True)
    write_emuir_net_map(path, tmp_path/"reference.tsv", _ir=ir)
    with closing(IndexedTimingView(path)) as view, \
         patch.object(IndexedTimingView, "timing_connections", side_effect=AssertionError("Python endpoints")), \
         patch("emuflow.timing.indexed_timing._timing_instances", side_effect=AssertionError("Python cells")), \
         patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("Python graph")):
        report = export_opensta_inputs(view, verilog=tmp_path/"actual.v", net_map=tmp_path/"actual.tsv")
    assert report["implementation_id"] == "native-opensta-export-v1"
    assert report["cells"] == len(ir.value["instances"])
    assert report["endpoints"] == sum(len(n["drivers"])+len(n["sinks"]) for n in ir.value["nets"])
    assert (tmp_path/"actual.v").read_text() == expected
    assert (tmp_path/"actual.tsv").read_bytes() == (tmp_path/"reference.tsv").read_bytes()


def test_sparse_bus_constants_inout_and_clock_polarity_match_independent_renderer(tmp_path):
    value = {"schema": "emuflow.emuir/v1", "design": {"name": "fixture", "top": "top/é", "source_format": "test"},
             "ports": [{"id": "bus", "name": "bus", "width": 5, "direction": "inout", "constant_connections": [{"bit": 4, "value": "z"}, {"bit": 2, "value": "1"}]}],
             "instances": [], "nets": [], "clocks": [], "warnings": []}
    parameters = [0, 1, True, False, 1.0, None, "01", "10", "0001", " 1 ", "+1", "0_1", "1_0", "invalid", ""]
    for i, param in enumerate(parameters):
        owner = f"dut/ff\\Qé{i}"
        value["instances"].append({"id": owner, "type": "FDRE", "resources": {"ff": 1}, "parameters": {"IS_C_INVERTED": param},
                                   "constant_connections": [{"port": "D", "bit": 4, "value": "0"}, {"port": "CE", "bit": 0, "value": "1"}]})
        value["nets"].append({"id": f"n{i}é", "drivers": [{"instance": owner, "port": "Q", "bit": 0}, {"instance": None, "port": "bus", "bit": i%5}],
                              "sinks": [{"instance": owner, "port": "D", "bit": 1}, {"instance": owner, "port": "R", "bit": 0}, {"instance": None, "port": "bus", "bit": i%5}], "cut_class": "multi_driver"})
    ir = EmuIR(value);path = tmp_path/"fixture.emuir.sqlite";ir.save(path)
    policies = [None, {i["id"]: _TimingTypes.for_instance(i) for i in value["instances"]}]
    with closing(IndexedTimingView(path)) as view:
        for i, policy in enumerate(policies):
            destination = tmp_path/f"timing-{i}.v"
            export_opensta_inputs(view, verilog=destination, timing_cell_types=view.timing_types() if policy else None)
            assert destination.read_text() == mapped_verilog(ir, timing_only=True, timing_cell_types=policy)


@pytest.mark.parametrize("damage", ["owner", "pin", "bit", "role", "net", "count", "cell-seq", "net-seq", "constants", "crc", "generation"])
def test_corruption_restores_previous_text_outputs(tmp_path, damage):
    _ir, path = counter(tmp_path)
    rtl, nets = tmp_path/"timing.v", tmp_path/"nets.tsv"
    rtl.write_text("prior verilog");nets.write_text("prior map")
    sql = {
        "owner": "UPDATE endpoint_records SET instance_seq=99999 WHERE instance_seq IS NOT NULL",
        "pin": "UPDATE endpoint_records SET port_seq=99999 WHERE instance_seq IS NOT NULL",
        "bit": "UPDATE endpoint_records SET bit=-1 WHERE instance_seq IS NOT NULL",
        "role": "UPDATE endpoint_records SET role=2 WHERE role=0",
        "net": "UPDATE endpoint_records SET net_seq=net_seq+100000",
        "count": "UPDATE metadata SET value=json_set(value,'$.cell_records',99999) WHERE key='record_counts_json'",
        "cell-seq": "UPDATE cell_records SET seq=seq+100000",
        "net-seq": "UPDATE net_records SET seq=seq+100000",
        "constants": "UPDATE payloads SET value='[123]' WHERE seq=(SELECT constants_seq FROM cell_records LIMIT 1)",
    }
    if damage in sql:
        with sqlite3.connect(path) as db:db.execute(sql[damage])
    from emuflow.timing import opensta_export_native as native
    finish = native.CheckedRequestWriter.finish
    def corrupt(writer):
        p = Path(writer.stream.name);finish(writer);data = bytearray(p.read_bytes());data[-1] ^= 1;p.write_bytes(data)
    with closing(IndexedTimingView(path)) as view:
        if damage == "generation":path.chmod(path.stat().st_mode ^ 0o100)
        with patch.object(native.CheckedRequestWriter, "finish", corrupt if damage == "crc" else finish), pytest.raises(ValidationError):
            export_opensta_inputs(view, verilog=rtl, net_map=nets)
    assert rtl.read_text() == "prior verilog"
    assert nets.read_text() == "prior map"
    assert not list(tmp_path.glob(".opensta-export.*"))
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("after", [False, True])
def test_group_replacement_restores_both_outputs(tmp_path, after):
    from emuflow.storage import store_publication
    _ir, path = counter(tmp_path)
    rtl, nets = tmp_path/"timing.v", tmp_path/"nets.tsv"
    rtl.write_text("old rtl");nets.write_text("old map")
    replace = store_publication.os.replace;failed = []
    def reject(src, dst):
        if Path(dst) == nets and not failed:
            failed.append(True)
            if after:replace(src, dst)
            raise OSError("injected map commit failure")
        return replace(src, dst)
    with closing(IndexedTimingView(path)) as view, patch.object(store_publication.os, "replace", reject), pytest.raises(OSError, match="injected"):
        export_opensta_inputs(view, verilog=rtl, net_map=nets)
    assert failed
    assert rtl.read_text() == "old rtl" and nets.read_text() == "old map"


def test_direct_writers_use_one_requested_output_and_reject_foreign_policy(tmp_path):
    ir, path = counter(tmp_path)
    with closing(IndexedTimingView(path)) as view:
        write_mapped_verilog(view, tmp_path/"single.v", timing_only=True)
        write_emuir_net_map(path, tmp_path/"single.tsv", _ir=view)
        assert mapped_verilog(view, timing_only=True) == mapped_verilog(ir, timing_only=True)
        with pytest.raises(ValidationError, match="source-bound"):
            export_opensta_inputs(view, verilog=tmp_path/"bad.v", timing_cell_types={})
        with pytest.raises(ValidationError, match="aliases a source"):
            export_opensta_inputs(view, verilog=path)
        with pytest.raises(ValidationError, match="alias each other"):
            export_opensta_inputs(view, verilog=tmp_path/"same", net_map=tmp_path/"same")
        with pytest.raises(ValidationError, match="another EmuIR"):
            write_emuir_net_map(tmp_path/"foreign.emuir.sqlite", tmp_path/"bad-map", _ir=view)
    with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("standalone graph load")):
        write_emuir_net_map(path, tmp_path/"standalone.tsv")
    assert (tmp_path/"standalone.tsv").read_bytes() == (tmp_path/"single.tsv").read_bytes()


def test_missing_native_export_is_rejected_before_frontend_work(tmp_path):
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    def require(schema):
        if schema == "opensta-input-export-request-v1":
            raise EmuFlowError("missing native timing export")
    with patch("emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract"), \
         patch("emuflow.common.native_tools.require_native_schema", side_effect=require), \
         patch("emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("synthesis started")), \
         pytest.raises(EmuFlowError, match="missing native timing export"):
        run_preflight(sources=[tmp_path/"absent.v"], top="dut", clocks=["clk"],
                           clock_periods={"clk": 10.0}, output_dir=tmp_path/"flow")
    assert not (tmp_path/"flow").exists()


def test_large_unique_cell_population_keeps_host_memory_bounded(tmp_path):
    value = {"schema": "emuflow.emuir/v1", "design": {"name": "scale", "top": "scale", "source_format": "test"},
             "ports": [], "instances": [], "nets": [], "clocks": [], "warnings": []}
    count = 20000
    for i in range(count):
        owner = "dut/unique/"+str(i)
        value["instances"].append({"id": owner, "type": "LUT1", "resources": {"lut": 1}, "parameters": {}, "constant_connections": []})
        value["nets"].append({"id": "n"+str(i), "drivers": [{"instance": owner, "port": "O", "bit": 0}], "sinks": [{"instance": owner, "port": "I0", "bit": 0}], "cut_class": "combinational"})
    path = tmp_path/"scale.emuir.sqlite";EmuIR(value).save(path);del value
    with closing(IndexedTimingView(path)) as view:
        tracemalloc.start()
        try:
            report = export_opensta_inputs(view, verilog=tmp_path/"scale.v", net_map=tmp_path/"scale.tsv")
            _current, peak = tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
    assert report["cells"] == report["nets"] == count
    assert report["endpoints"] == count*2 and report["maximum_instance_pins"] == 2
    assert peak < 4*1024*1024
    assert (tmp_path/"scale.v").read_text().count("  \\LUT1 ") == count
    assert sum(1 for _line in (tmp_path/"scale.tsv").open()) == count+1
