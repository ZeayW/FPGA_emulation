from tests.compile_adapter_fixture import preflight as run_preflight
"""Direct graph/model staging contracts; unit fixtures are not NVDLA qualification."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import tracemalloc
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.timing.indexed_timing import IndexedTimingView
from emuflow.timing.opensta import validate_timing_model_coverage
from tests.routed_opensta_stage_reference import RoutedTimingModel
from tests.routed_opensta_reference import build_xilinx_routed_opensta_indexed_inputs
from tests.test_xilinx_opensta import _indexed_routed_fixture


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def fixture(tmp_path):
    mapped, timing = _indexed_routed_fixture(tmp_path)
    output = tmp_path / "native.emuir.sqlite"
    return mapped, timing, output


def rewrite(timing, transform):
    value = json.loads(timing.read_text())
    payload = timing.parent / value["payloads"]["endpoints"]["path"]
    records = [json.loads(line) for line in payload.read_text().splitlines()]
    transform(value, records)
    payload.write_bytes(b"".join(json.dumps(row).encode()+b"\n" for row in records))
    value["payloads"]["endpoints"].update(records=len(records), sha256=file_sha256(payload))
    value["summary"].update(logical_endpoints=len(records), maximum_route_delay_ns=max((r["route_delay_ns"] for r in records), default=0))
    timing.write_text(json.dumps(value))


def stage(mapped, timing, output):
    return build_xilinx_routed_opensta_indexed_inputs(mapped, timing, output, clocks={"clk": 10.0})


def test_native_stages_without_python_endpoints_cells_or_complete_model(tmp_path):
    mapped, timing, output = fixture(tmp_path)
    with patch("tests.routed_timing_adapter.iter_xilinx_routed_timing_endpoints", side_effect=AssertionError("Python endpoint population")), \
         patch("emuflow.frontend.yosys.import_yosys_json_indexed", side_effect=AssertionError("Python staging graph")), \
         patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("Python graph materialization")):
        model, report = stage(mapped, timing, output)
    assert isinstance(model, RoutedTimingModel)
    assert not isinstance(model["cells"], dict)
    assert report["implementation_id"] == "native-routed-opensta-v1"
    assert report["native_metrics"]["wall_seconds"] >= 0
    assert len(model.base_model["cells"]) < 100
    with closing(IndexedTimingView(output)) as ir, patch.object(type(model["cells"]), "items", side_effect=AssertionError("model population")):
        coverage = validate_timing_model_coverage(ir, model)
    assert coverage["used_cell_type_count"] == 3
    assert coverage["generated_cell_types"]["records"] == 1
    assert coverage["generated_cell_types"]["artifact_generation"] == list(model.owner.generation)
    assert output.with_name(output.name+".route-delays.lib").read_text().count("cell (EMUFLOW_RW_ROUTE_DELAY_") == 1


def test_empty_endpoint_population_is_an_explicit_zero_delay_model(tmp_path):
    mapped, timing, output = fixture(tmp_path)
    rewrite(timing, lambda _value, records: records.clear())
    model, report = stage(mapped, timing, output)
    assert report["inserted_route_delay_cells"] == model.delay_count == 0
    with closing(IndexedTimingView(output)) as ir:
        coverage = validate_timing_model_coverage(ir, model)
    assert coverage["generated_cell_types"]["records"] == 0
    assert dict(model["cells"]) == model.base_model["cells"]


def test_indexed_runner_keeps_native_coverage_bounded_in_its_final_report(tmp_path):
    from emuflow.timing.opensta import run_opensta_path_database
    mapped, timing, output = fixture(tmp_path)
    model, _ = stage(mapped, timing, output)

    def engine(argv, *, environment):
        assert environment["EMUFLOW_STA_EXTRA_LIBERTY"] == str(model.library.path)
        fields = ["path_id_hex", "clock_domain_hex", "clock_period_ns", "slack_ns",
                  "fixed_delay_ns", "path_nets_hex"]
        row = ["path".encode().hex(), "clk".encode().hex(), "10", "9.9", "0.1", "n".encode().hex()]
        Path(environment["EMUFLOW_STA_OUTPUT"]).write_text("\t".join(fields)+"\n"+"\t".join(row)+"\n")
        return subprocess.CompletedProcess(argv, 0, stdout="test engine")

    with patch("emuflow.timing.opensta.resolve_native_executable", return_value="test-sta"), \
         patch("emuflow.timing.opensta.require_opensta_engine", return_value={"version": "test"}), \
         patch("emuflow.timing.opensta.run_with_bounded_output", side_effect=engine), \
         patch("emuflow.timing.opensta._sealed_string_set_summary", side_effect=AssertionError("expanded model summary")), \
         patch.object(type(model["cells"]), "items", side_effect=AssertionError("expanded model")):
        report = run_opensta_path_database(output, tmp_path/"paths.json", clocks={"clk": 10},
                                          _timing_model=model)
    summary = report["used_cell_type_summary"]
    assert report["input_export"]["implementation_id"] == "native-opensta-export-v1"
    assert report["input_export"]["native_metrics"]["wall_seconds"] >= 0
    assert report["paths"] == 1
    assert summary["schema"] == "emuflow.native-routed-model-coverage/v1"
    assert summary["count"] == 3
    assert summary["generated_cell_types"]["records"] == 1
    assert summary["generated_cell_types"]["artifact_generation"] == list(model.owner.generation)
    assert set(summary["base_cell_types"]) == {"FDRE", "LUT1"}


@pytest.mark.parametrize("damage", ["duplicate-id", "duplicate-sink", "wrong-bit", "output-pin", "constant-pin", "negative", "nonfinite", "bad-coefficient", "io-duplicate", "io-measurement", "truncated", "crc"])
def test_rejected_inputs_preserve_both_previous_outputs(tmp_path, damage):
    from tests import routed_opensta_stage_reference as native
    mapped, timing, output = fixture(tmp_path)
    stage(mapped, timing, output)
    library = output.with_name(output.name+".route-delays.lib")
    previous = {path: path.read_bytes() for path in (output, library)}
    def change(value, records):
        if damage in {"duplicate-id", "duplicate-sink"}:
            row = json.loads(json.dumps(records[0]))
            if damage == "duplicate-sink":
                row["id"] = "other-id"
            else:
                row["sink"].update(instance="lut", pin="I0")
                row["mapped_bit"] = 3
            records.append(row)
        elif damage == "wrong-bit":records[0]["mapped_bit"] = 99
        elif damage == "output-pin":records[0]["sink"]["pin"] = "Q"
        elif damage == "constant-pin":records[0]["sink"]["pin"] = "CE"
        elif damage == "negative":records[0]["route_delay_ns"] = -1
        elif damage == "nonfinite":records[0]["route_delay_ns"] = float("inf")
        elif damage == "bad-coefficient":value["qualification"]["logic_coefficients_ps"]["lut_a1"] = True
        elif damage.startswith("io-"):
            row = {"id": "io", "logical_port": "a", "logical_bit": 0, "direction": "tx", "p_site": "P", "n_site": "N", "route_delay_ns": 0.0, "measurement": "fixed-iob-routed-boundary"}
            value["virtual_io"] = [row, dict(row)] if damage == "io-duplicate" else [{**row, "measurement": "wrong"}]
    rewrite(timing, change)
    if damage == "truncated":
        payload = timing.parent / json.loads(timing.read_text())["payloads"]["endpoints"]["path"]
        payload.write_bytes(payload.read_bytes()[:-1])
    finish = native.CheckedRequestWriter.finish
    def corrupt(writer):
        path = Path(writer.stream.name)
        finish(writer)
        data = bytearray(path.read_bytes());data[-1] ^= 1;path.write_bytes(data)
    with patch.object(native.CheckedRequestWriter, "finish", corrupt if damage == "crc" else finish):
        with pytest.raises(ValidationError):
            stage(mapped, timing, output)
    for path, data in previous.items():
        assert path.read_bytes() == data
    assert not list(tmp_path.glob(".routed-opensta.*"))
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("damage", ["owner", "width", "record-type"])
def test_typed_pin_and_record_contracts_are_checked_during_the_native_scan(tmp_path, damage):
    mapped, timing, output = fixture(tmp_path)
    stage(mapped, timing, output)
    before = output.read_bytes()
    with sqlite3.connect(mapped) as db:
        if damage == "owner":db.execute("UPDATE cell_pins SET cell_seq=99999 WHERE cell_id='ff' AND port='CE'")
        elif damage == "width":db.execute("UPDATE cell_pins SET port_width=2 WHERE cell_id='ff' AND port='CE'")
        else:db.execute("UPDATE cells SET record_json=json_set(record_json,'$.type','FDCE') WHERE id='ff'")
    value = json.loads(timing.read_text())
    value["source"]["mapped_sha256"] = file_sha256(mapped)
    timing.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="native routed OpenSTA staging failed"):
        stage(mapped, timing, output)
    assert output.read_bytes() == before


@pytest.mark.parametrize("after", [False, True])
def test_group_publication_restores_old_graph_and_library(tmp_path, after):
    from emuflow.storage import store_publication
    mapped, timing, output = fixture(tmp_path)
    model, _ = stage(mapped, timing, output)
    library = model.library.path
    previous = {path: path.read_bytes() for path in (output, library)}
    rewrite(timing, lambda _value, records: records[0].update(route_delay_ns=0.321))
    replace = store_publication.os.replace
    failed = []
    def reject(src, dest):
        if Path(dest).resolve() == library and not failed:
            failed.append(True)
            if after:replace(src, dest)
            raise OSError("injected library publication failure")
        return replace(src, dest)
    with patch.object(store_publication.os, "replace", reject), pytest.raises(OSError, match="injected"):
        stage(mapped, timing, output)
    assert failed
    for path, data in previous.items():assert path.read_bytes() == data
    assert not list(tmp_path.glob("*.tmp"))


def test_borrowed_model_rejects_replacement_and_wrong_graph(tmp_path):
    mapped, timing, output = fixture(tmp_path)
    model, _ = stage(mapped, timing, output)
    other = tmp_path / "other.emuir.sqlite"
    stage(mapped, timing, other)
    with closing(IndexedTimingView(other)) as ir, pytest.raises(ValidationError, match="another graph"):
        validate_timing_model_coverage(ir, model)
    model.library.path.chmod(model.library.path.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match="generation changed"):
        model["cells"]["EMUFLOW_RW_ROUTE_DELAY_000000"]


def test_bound_model_cannot_fall_back_to_inline_graph_loading(tmp_path):
    from emuflow.timing.opensta import run_opensta_path_database
    mapped, timing, output = fixture(tmp_path)
    model, _ = stage(mapped, timing, output)
    from emuflow.ir.ir import EmuIR
    from emuflow.ir.emuir_storage import _write_emuir_sqlite_v1
    legacy = tmp_path/"legacy.emuir.sqlite"
    _write_emuir_sqlite_v1(legacy, EmuIR.load(output).value)
    with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole graph fallback")), \
         pytest.raises(ValidationError, match="indexed streaming"):
        run_opensta_path_database(legacy, tmp_path / "paths.json", _timing_model=model)


def test_current_opensta_cli_uses_the_typed_source(tmp_path):
    from emuflow.flow.cli import main
    args = ["arch", "run-xilinx-opensta", "--mapped-index", str(tmp_path / "mapped.sqlite"),
            "--routed-timing", str(tmp_path / "timing.json"), "--output", str(tmp_path / "paths.json"),
            "--summary", str(tmp_path / "summary.json"), "--clock-period", "clk=10"]
    with patch("emuflow.flow.cli.run_xilinx_routed_opensta", return_value={"status": "pass"}) as run:
        assert main(args) == 0
    assert run.call_args.args[:2] == (tmp_path / "mapped.sqlite", tmp_path / "timing.json")
    with pytest.raises(SystemExit):
        main(["arch", "run-xilinx-opensta", "--mapped-json", "retired.json", "--routed-timing", "t.json", "--output", "p.json", "--summary", "s.json", "--clock-period", "clk=10"])


def test_missing_staging_capability_fails_before_synthesis(tmp_path):
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    def require(schema):
        if schema == "routed-opensta-request-v1":
            raise EmuFlowError("missing native routed staging")
    with patch("emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract"), \
         patch("emuflow.common.native_tools.require_native_schema", side_effect=require), \
         patch("emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("synthesis started")), \
         pytest.raises(EmuFlowError, match="missing native routed staging"):
        run_preflight(sources=[tmp_path / "absent.v"], top="dut", clocks=["clk"], clock_periods={"clk": 10.0},
                           physical=True, physical_backend="rapidwright", output_dir=tmp_path / "flow")
    assert not (tmp_path / "flow").exists()


def test_direct_twenty_thousand_unique_delays_keep_python_bounded(tmp_path):
    mapped, timing, output = fixture(tmp_path)
    count = 20000
    with sqlite3.connect(mapped) as db:
        seq = db.execute("SELECT max(seq)+1 FROM cells").fetchone()[0]
        for ordinal in range(1, count):
            name = f"clone_{ordinal:06d}"
            db.execute("INSERT INTO cells SELECT ?,?,type,record_json FROM cells WHERE id='ff'", (name, seq))
            db.execute("INSERT INTO cell_pins SELECT ?,?,port,port_bit,port_width,direction,value_kind,net_bit,constant FROM cell_pins WHERE cell_id='ff' AND port!='Q'", (seq, name))
            seq += 1
    value = json.loads(timing.read_text());value["source"]["mapped_sha256"] = file_sha256(mapped)
    payload = timing.parent / value["payloads"]["endpoints"]["path"]
    with payload.open("w") as stream:
        for ordinal in range(count):
            name = "ff" if ordinal == 0 else f"clone_{ordinal:06d}"
            stream.write(json.dumps({"id": f"n4:{name}/D", "net": "n4", "mapped_bit": 4, "driver": {"instance": "lut", "pin": "O", "site": "S0"}, "sink": {"instance": name, "pin": "D", "site": "S1"}, "route_delay_ns": ordinal/100000, "binding": "exact-site-pin"})+"\n")
    value["payloads"]["endpoints"].update(records=count,sha256=file_sha256(payload))
    value["summary"].update(logical_endpoints=count,maximum_route_delay_ns=(count-1)/100000)
    timing.write_text(json.dumps(value));del value
    tracemalloc.start()
    try:
        with patch("tests.routed_timing_adapter.iter_xilinx_routed_timing_endpoints", side_effect=AssertionError("Python endpoint population")):
            model, report = stage(mapped, timing, output)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16*1024*1024
    assert report["inserted_route_delay_cells"] == report["unique_route_delay_cells"] == count
    with sqlite3.connect(output) as db:
        # Every original capture pin must receive its own measured delay cell.
        bound = db.execute("""SELECT count(*) FROM cell_records c
           JOIN endpoint_records sink ON sink.instance_seq=c.seq AND sink.role=1
           JOIN pin_names sink_pin ON sink_pin.seq=sink.port_seq AND sink_pin.name='D'
           JOIN endpoint_records driver ON driver.net_seq=sink.net_seq AND driver.role=0
           JOIN cell_records delay ON delay.seq=driver.instance_seq
           JOIN pin_names driver_pin ON driver_pin.seq=driver.port_seq AND driver_pin.name='Y'
           JOIN payloads attrs ON attrs.seq=delay.attributes_seq
          WHERE (c.id='ff' OR c.id GLOB 'clone_[0-9]*')
            AND delay.id=printf('__emuflow_rw_delay__%08d',CASE WHEN c.id='ff' THEN 0 ELSE CAST(substr(c.id,7) AS INTEGER) END)
            AND abs(json_extract(attrs.value,'$.emuflow_route_delay_ns')-(CASE WHEN c.id='ff' THEN 0 ELSE CAST(substr(c.id,7) AS INTEGER) END)/100000.0)<1e-12""").fetchone()[0]
    assert bound == count
    with closing(IndexedTimingView(output)) as ir, patch.object(type(model["cells"]), "items", side_effect=AssertionError("Python type population")):
        coverage = validate_timing_model_coverage(ir, model)
    assert coverage["generated_cell_types"]["records"] == count
    assert len(json.dumps(coverage)) < 16384
