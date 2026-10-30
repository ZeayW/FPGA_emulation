"""Native path import/audit equivalence, ownership and scale; not NVDLA acceptance."""
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import tracemalloc
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.timing.indexed_timing import IndexedTimingView
from emuflow.common.json_io import file_sha256
from emuflow_tools.stage_clients.opensta import OPENSTA_PROVIDER, run_opensta_path_database
from emuflow_tools.timing.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming, iter_sta_path_database_paths, validate_sta_path_database
from tests.yosys_reference import import_yosys_json
from tests.sta_path_reference import endpoint_facts
from tests.sta_path_reference import path_facts

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def fixture(tmp_path):
    ir = import_yosys_json(ROOT/"examples/yosys/counter.json", top="counter", clocks=["clk"])
    path = tmp_path/"ir ?#%é.emuir.sqlite";ir.save(path)
    objects = sorted(endpoint_facts(ir))
    nets = [n["id"] for n in ir.value["nets"]]
    records = []
    for i, slack in enumerate((1.5, -1e16, -1.0, -1e-16, 0.0, -2.3, 1e-30)):
        identity = f"{objects[i%len(objects)]}->{objects[-1]}#{i:08d}" if i%2 else f"legacy-{i}"
        records.append([identity.encode().hex(), "clk".encode().hex(), "10", str(slack), "0.25",
                        ",".join(n.encode().hex() for n in nets[:1+i%len(nets)])])
    measured = tmp_path/"paths.tsv"
    write(measured, records)
    return ir, path, measured, records


def write(path, records):
    path.write_text(STA_PATH_DATABASE_TSV_HEADER+"\n"+"\n".join("\t".join(row) for row in records)+"\n")


def produce(measured, source, destination, view=None):
    return import_sta_path_database_tsv_streaming(measured, source, destination, provider=OPENSTA_PROVIDER, _ir=view)


def test_final_records_and_exact_reduction_match_independent_producer(tmp_path):
    ir, source, measured, _records = fixture(tmp_path)
    new = tmp_path/"new.json"
    expected = path_facts(measured, ir)
    from emuflow_tools.timing import sta
    assert not hasattr(sta, "_parse_sta_path_database_tsv_record")
    assert not hasattr(sta, "sta_object_index")
    with closing(IndexedTimingView(source)) as view, \
         patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("Python graph")):
        result = produce(measured, source, new, view)
    actual = json.loads(new.read_text())
    assert actual["normalization"] == expected["normalization"]
    assert actual["summary"] == expected["summary"]
    assert list(iter_sta_path_database_paths(new, actual)) == expected["paths"]
    assert result["implementation_id"] == "native-sta-path-import-v1"
    with patch("emuflow_tools.timing.sta.iter_sta_path_database_paths", side_effect=AssertionError("Python path validation")):
        checked = validate_sta_path_database(new, source)
    assert checked["path_qor"] == actual["summary"]["qor"]


def test_normal_consumer_reuses_native_proof_without_rescanning_rows(tmp_path):
    _ir, source, _measured, _records = fixture(tmp_path)
    with patch("emuflow_tools.timing.sta_paths_native.validate_paths", side_effect=AssertionError("redundant native scan")), \
         patch("emuflow_tools.timing.sta._validate_sta_path_database_stream", side_effect=AssertionError("Python validator")):
        report = run_opensta_path_database(source, tmp_path/"engine.json", clocks={"clk": 10},
            executable=str(ROOT/"tests/fixtures/fake_opensta_paths.py"))
    assert report["checker"]["status"] == "pass"
    assert report["path_import"]["implementation_id"] == "native-sta-path-import-v1"


def test_escaped_cell_and_bus_provider_spellings_match_reference_certificates(tmp_path):
    ir = import_yosys_json(ROOT/"examples/yosys/counter.json", top="counter", clocks=["clk"])
    renamed = {item["id"]: "escaped\\"+item["id"] for item in ir.value["instances"]}
    for item in ir.value["instances"]:
        item["id"] = renamed[item["id"]]
    for net in ir.value["nets"]:
        for endpoint in (*net["drivers"], *net["sinks"]):
            if endpoint["instance"] is not None:
                endpoint["instance"] = renamed[endpoint["instance"]]
    ir.value["ports"].append({"id": "bus\\wire", "direction": "input", "width": 3})
    source = tmp_path/"escaped.emuir.sqlite";ir.save(source)
    index = endpoint_facts(ir)
    internal = next(name for name in sorted(index) if "/" in name and name == index[name]["object"])
    pairs = [(internal, "bus\\wire[2]"), (internal.replace("\\", "\\\\"), "bus\\wire__2".replace("\\", "\\\\"))]
    measured = tmp_path/"escaped.tsv"
    write(measured, [[f"{start}->{end}#{i:08d}".encode().hex(), b"clk".hex(), "10", "-0.5", "0", ir.value["nets"][0]["id"].encode().hex()] for i, (start, end) in enumerate(pairs)])
    new = tmp_path/"new.json"
    expected = path_facts(measured, ir)
    produce(measured, source, new)
    assert list(iter_sta_path_database_paths(new, json.loads(new.read_text()))) == expected["paths"]


@pytest.mark.parametrize("damage", ["duplicate-id", "duplicate-net", "unknown-net", "empty-net", "bad-hex", "nonfinite", "period", "negative-delay", "empty", "crc", "truncated", "version", "generation"])
def test_rejected_source_keeps_previous_selector_and_payload(tmp_path, damage):
    _ir, source, measured, records = fixture(tmp_path)
    destination = tmp_path/"result.json";produce(measured, source, destination)
    manifest = json.loads(destination.read_text());payload = destination.parent/manifest["payloads"]["paths"]["path"]
    previous = destination.read_bytes(), payload.read_bytes()
    if damage == "duplicate-id":records.append(list(records[0]))
    elif damage == "duplicate-net":records[0][5] += ","+records[0][5]
    elif damage == "unknown-net":records[0][5] = b"unknown".hex()
    elif damage == "empty-net":records[0][5] = ""
    elif damage == "bad-hex":records[0][0] = "ff"
    elif damage == "nonfinite":records[0][3] = "inf"
    elif damage == "period":records[0][2] = "0"
    elif damage == "negative-delay":records[0][4] = "-1"
    elif damage == "empty":records.clear()
    write(measured, records)
    from emuflow_tools.timing import sta_paths_native as native
    finish = native.CheckedRequestWriter.finish
    def corrupt(writer):
        path = Path(writer.stream.name);finish(writer)
        if damage == "generation":source.chmod(source.stat().st_mode ^ 0o100);return
        data = bytearray(path.read_bytes())
        if damage == "truncated":data = data[:20]
        elif damage == "version":data[8] ^= 1
        else:data[-1] ^= 1
        path.write_bytes(data)
    with patch.object(native.CheckedRequestWriter, "finish", corrupt if damage in {"crc", "truncated", "version", "generation"} else finish), pytest.raises(ValidationError):
        produce(measured, source, destination)
    assert destination.read_bytes() == previous[0] and payload.read_bytes() == previous[1]
    assert not list(tmp_path.glob(".sta-path-import.*")) and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("damage", ["orphan", "net-order", "net-name", "path-order", "missing-certificate", "instance", "port", "bit", "object", "delay", "index", "summary", "normalization", "digest"])
def test_independent_validator_rejects_damaged_final_owner(tmp_path, damage):
    _ir, source, measured, _records = fixture(tmp_path)
    destination = tmp_path/"result.json";produce(measured, source, destination)
    value = json.loads(destination.read_text());payload = destination.parent/value["payloads"]["paths"]["path"]
    sql = {
        "orphan": "UPDATE path_nets SET path_ordinal=path_ordinal+10000",
        "net-order": "UPDATE path_nets SET net_ordinal=net_ordinal+10",
        "net-name": "UPDATE path_nets SET net_id='unknown'",
        "path-order": "UPDATE paths SET ordinal=ordinal+10000",
        "missing-certificate": "UPDATE paths SET end_port=NULL WHERE start_object IS NOT NULL",
        "instance": "UPDATE paths SET start_instance='unknown' WHERE start_object IS NOT NULL",
        "port": "UPDATE paths SET start_port='unknown' WHERE start_object IS NOT NULL",
        "bit": "UPDATE paths SET start_bit=start_bit+100 WHERE start_object IS NOT NULL",
        "object": "UPDATE paths SET start_object='unknown' WHERE start_object IS NOT NULL",
        "delay": "UPDATE paths SET fixed_delay_ns=-1",
        "index": "DROP INDEX path_nets_net_id",
    }
    if damage in sql:
        with sqlite3.connect(payload) as db:db.execute(sql[damage])
        value["payloads"]["paths"]["sha256"] = file_sha256(payload)
    elif damage == "summary":value["summary"]["qor"]["tns_ns"] = 0
    elif damage == "normalization":value["normalization"]["negative_slack_scale_ns"] = 0
    else:value["payloads"]["paths"]["sha256"] = "0"*64
    destination.write_text(json.dumps(value))
    with pytest.raises(ValidationError):validate_sta_path_database(destination, source)


def test_validation_lease_rejects_changed_owner_and_in_memory_metadata(tmp_path):
    _ir, source, measured, _records = fixture(tmp_path)
    destination = tmp_path/"result.json"
    with closing(IndexedTimingView(source)) as view:
        result = produce(measured, source, destination, view);value = json.loads(destination.read_text())
        assert result.validation.require(destination, value, view)["status"] == "pass"
        altered = json.loads(json.dumps(value));altered["summary"]["paths"] = True
        with pytest.raises(ValidationError, match="another owner"):
            result.validation.require(destination, altered, view)
        payload = destination.parent/value["payloads"]["paths"]["path"];payload.chmod(payload.stat().st_mode ^ 0o100)
        with pytest.raises(ValidationError, match="generation changed"):
            result.validation.require(destination, value, view)


def test_many_distinct_paths_and_nets_do_not_grow_python_populations(tmp_path):
    from emuflow.ir.ir import EmuIR
    value = {"schema": "emuflow.emuir/v1", "design": {"name": "scale", "top": "scale", "source_format": "test"},
             "instances": [], "nets": [], "ports": [], "clocks": [], "warnings": []}
    count = 30000
    for i in range(count):value["nets"].append({"id": "net/"+str(i), "drivers": [], "sinks": [], "cut_class": "undriven"})
    source = tmp_path/"scale.emuir.sqlite";EmuIR(value).save(source);del value
    measured = tmp_path/"scale.tsv";negative = [-1e16, -1.0, -1e-16, -2.3]
    with measured.open('w') as stream:
        stream.write(STA_PATH_DATABASE_TSV_HEADER+"\n")
        for i in range(count):stream.write("\t".join([("unique-path/"+str(i)).encode().hex(), b"clk".hex(), "10", str(negative[i%4]), "0.25", ("net/"+str(i)).encode().hex()])+"\n")
    destination = tmp_path/"scale.json"
    with closing(IndexedTimingView(source)) as view:
        tracemalloc.start()
        try:
            report = produce(measured, source, destination, view)
            checked = validate_sta_path_database(destination, source)
            _current, peak = tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
    assert report["paths"] == checked["unique_path_nets"] == count
    assert checked["path_qor"]["tns_ns"] == math.fsum(negative[i%4] for i in range(count))
    assert peak < 5*1024*1024


def test_unreached_wide_top_port_does_not_expand_for_internal_endpoint_queries(tmp_path):
    from emuflow.common.tool_runtime import NativeToolProcess

    ir = import_yosys_json(ROOT/"examples/yosys/counter.json", top="counter", clocks=["clk"])
    objects = sorted(name for name in endpoint_facts(ir) if "/" in name)
    ir.value["ports"].append({"id": "unreached", "direction": "input", "width": 10000000})
    source = tmp_path/"wide.emuir.sqlite";ir.save(source)
    measured = tmp_path/"wide.tsv"
    identity = f"{objects[0]}->{objects[-1]}#00000000"
    write(measured, [[identity.encode().hex(), b"clk".hex(), "10", "1", "0", ir.value["nets"][0]["id"].encode().hex()]])

    def bounded_process(*args, **kwargs):
        return NativeToolProcess(*args, **kwargs, timeout_ms=15000)

    with patch("emuflow_tools.timing.sta_paths_native.NativeToolProcess", side_effect=bounded_process):
        report = produce(measured, source, tmp_path/"paths.json")
    assert report["structured_endpoint_paths"] == 1
