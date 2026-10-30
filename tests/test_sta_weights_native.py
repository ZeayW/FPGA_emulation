"""Whole native weight producer/source auditor, not real-design qualification."""
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
from emuflow.common.json_io import file_sha256
from emuflow_tools.timing.sta import derive_partition_net_weights, validate_partition_net_weights, import_sta_path_database_tsv_streaming, iter_partition_net_weight_records
from tests.sta_weight_reference import derive_partition_net_weights as reference_build
from tests.sta_weight_reference import validate_partition_net_weights as reference_validate
from tests.test_sta_paths_native import fixture as path_fixture, write


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def inputs(tmp_path, positive=False):
    _ir, source, measured, rows = path_fixture(tmp_path)
    if positive:
        for i, row in enumerate(rows):
            row[3] = str(0.125+i/3 if positive == "fractional" else 11+i)
        write(measured, rows)
    selector = tmp_path/"paths.json"
    report = import_sta_path_database_tsv_streaming(measured, source, selector, provider="opensta-fpga-path-database-v1")
    return source, selector, report.validation


@pytest.mark.parametrize("scale,exponent,positive", [(9, 2, False), (3.125, 0.3, "fractional"), (1e20, 8.25, "fractional"), (9, 2, True)])
def test_complete_records_policy_and_summary_match_independent_oracle(tmp_path, scale, exponent, positive):
    source, paths, proof = inputs(tmp_path, positive)
    old, new = tmp_path/"old.json", tmp_path/"new.json"
    options = {"criticality_scale": scale, "criticality_exponent": exponent}
    reference_build(paths, source, old, **options)
    with patch("emuflow_tools.timing.sta.iter_sta_path_database_paths", side_effect=AssertionError("Python path replay")), \
         patch("emuflow_tools.timing.sta.iter_partition_net_weight_records", side_effect=AssertionError("Python weight population")), \
         patch("emuflow_tools.timing.sta.EmuIR.load", side_effect=AssertionError("Python graph")):
        report = derive_partition_net_weights(paths, source, new, **options)
        checked = validate_partition_net_weights(new, paths, source)
    expected, actual = json.loads(old.read_text()), json.loads(new.read_text())
    assert {k: actual[k] for k in ("schema", "design", "source", "parameters", "summary")} == {k: expected[k] for k in ("schema", "design", "source", "parameters", "summary")}
    assert list(iter_partition_net_weight_records(new)) == list(iter_partition_net_weight_records(old))
    assert checked == reference_validate(new, paths, source)
    assert report["implementation_id"] == "native-sta-net-weights-v1"


@pytest.mark.parametrize("damage", ["scale-zero", "scale-bool", "exponent-negative", "exponent-inf", "crc", "version", "generation", "overflow"])
def test_failed_producer_preserves_prior_generation(tmp_path, damage):
    from emuflow_tools.timing.sta_paths_native import CheckedRequestWriter

    source, paths, proof = inputs(tmp_path, "fractional" if damage == "overflow" else False)
    target = tmp_path/"weights.json";derive_partition_net_weights(paths, source, target)
    manifest = json.loads(target.read_text());payload = target.parent/manifest["payloads"]["weights"]["path"]
    previous = target.read_bytes(), payload.read_bytes()
    options = {"_validation": proof}
    if damage == "scale-zero":options["criticality_scale"] = 0
    elif damage == "scale-bool":options["criticality_scale"] = True
    elif damage == "exponent-negative":options["criticality_exponent"] = -1
    elif damage == "exponent-inf":options["criticality_exponent"] = math.inf
    elif damage == "overflow":options["criticality_scale"] = 10**400
    finish = CheckedRequestWriter.finish
    def broken(writer):
        path = Path(writer.stream.name);finish(writer)
        if damage == "generation":paths.chmod(paths.stat().st_mode ^ 0o100);return
        data = bytearray(path.read_bytes());data[8 if damage == "version" else -1] ^= 1;path.write_bytes(data)
    with patch.object(CheckedRequestWriter, "finish", broken if damage in {"crc", "version", "generation"} else finish), pytest.raises(ValidationError):
        derive_partition_net_weights(paths, source, target, **options)
    assert target.read_bytes() == previous[0] and payload.read_bytes() == previous[1]
    assert not list(tmp_path.glob(".sta-weight-build.*"))


@pytest.mark.parametrize("damage", ["criticality", "count", "weight", "missing", "extra", "summary", "records", "parameters", "policy", "source", "design", "format", "digest"])
def test_independent_source_auditor_rejects_corrupt_final_owner(tmp_path, damage):
    source, paths, proof = inputs(tmp_path)
    target = tmp_path/"weights.json";derive_partition_net_weights(paths, source, target)
    value = json.loads(target.read_text());payload = target.parent/value["payloads"]["weights"]["path"]
    sql = {"criticality": "UPDATE net_weights SET criticality=0.123", "count": "UPDATE net_weights SET path_count=path_count+1", "weight": "UPDATE net_weights SET weight=weight+1", "missing": "DELETE FROM net_weights WHERE net_id=(SELECT min(net_id) FROM net_weights)", "extra": "INSERT INTO net_weights VALUES('unknown',0,1,1)"}
    if damage in sql:
        with closing(sqlite3.connect(payload)) as db, db:db.execute(sql[damage])
        value["payloads"]["weights"]["sha256"] = file_sha256(payload)
    elif damage == "summary":value["summary"]["weighted_nets"] += 1
    elif damage == "records":value["payloads"]["weights"]["records"] += 1
    elif damage == "parameters":value["parameters"]["criticality_scale"] = True
    elif damage == "policy":value["parameters"]["path_reduction"] = "sum"
    elif damage == "source":value["source"]["path_database"] = "foreign.json"
    elif damage == "design":value["design"] = "foreign"
    elif damage == "format":value["payloads"]["weights"]["format"] = "legacy"
    else:value["payloads"]["weights"]["sha256"] = "0"*64
    target.write_text(json.dumps(value))
    with pytest.raises(ValidationError):validate_partition_net_weights(target, paths, source)


def test_changed_path_owner_is_not_rebound_as_prevalidated(tmp_path):
    source, paths, proof = inputs(tmp_path)
    manifest = json.loads(paths.read_text());payload = paths.parent/manifest["payloads"]["paths"]["path"]
    with closing(sqlite3.connect(payload)) as db, db:db.execute("UPDATE path_nets SET net_id='foreign'")
    manifest["payloads"]["paths"]["sha256"] = file_sha256(payload);paths.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):derive_partition_net_weights(paths, source, tmp_path/"weights.json", _validation=proof)
    assert not (tmp_path/"weights.json").exists()


def test_original_proof_reuse_does_not_repeat_full_path_validation(tmp_path):
    source, paths, proof = inputs(tmp_path)
    target = tmp_path/"weights.json"
    with patch("emuflow_tools.timing.sta_paths_native.validate_paths", side_effect=AssertionError("redundant path audit")):
        assert derive_partition_net_weights(paths, source, target, _validation=proof)["status"] == "pass"
    with pytest.raises(ValidationError, match="original typed path validation proof"):
        derive_partition_net_weights(paths, source, tmp_path/"invalid.json", _validation=True)


def test_optional_native_extraction_and_weight_chain_never_selects_inline_production(tmp_path):
    from emuflow.flow.cli import main
    from emuflow_tools.stage_clients.opensta import run_opensta_path_database
    from tests.test_sta_paths_native import ROOT

    _ir, source, _measured, _records = path_fixture(tmp_path)
    paths, weights = tmp_path/"engine-paths.json", tmp_path/"engine-weights.json"
    with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole-graph fallback")), \
         patch("emuflow_tools.timing.sta.iter_sta_path_database_paths", side_effect=AssertionError("Python path replay")):
        extracted = run_opensta_path_database(
            source, paths, executable=str(ROOT/"tests/fixtures/fake_opensta_paths.py"), clocks={"clk": 10.0},
        )
        assert extracted["paths"] > 0
        assert derive_partition_net_weights(paths, source, weights)["status"] == "pass"
    assert json.loads(paths.read_text())["schema"] == "emuflow.sta-path-database/v3"
    assert validate_partition_net_weights(weights, paths, source)["paths"] > 0
    for command in (["sta", "run-opensta", "--ir", str(source)],
                    ["sta", "derive-partition-net-weights", "--ir", str(source)]):
        with pytest.raises(SystemExit):
            main(command)


def test_legacy_ir_is_rejected_before_starting_opensta(tmp_path):
    from emuflow_tools.stage_clients.opensta import run_opensta_path_database

    ir, _source, _measured, _records = path_fixture(tmp_path)
    legacy = tmp_path/"legacy.json";legacy.write_text(json.dumps(ir.value))
    with patch("emuflow_tools.stage_clients.opensta.require_opensta_engine", side_effect=AssertionError("engine started")), \
         pytest.raises(ValidationError, match="compact EmuIR v2"):
        run_opensta_path_database(legacy, tmp_path/"paths.json")


def test_distinct_paths_and_nets_do_not_create_python_weight_populations(tmp_path):
    from emuflow.ir.ir import EmuIR
    from emuflow_tools.timing.sta import STA_PATH_DATABASE_TSV_HEADER

    count = 30000
    source = tmp_path/"large.emuir.sqlite"
    value = {"schema": "emuflow.emuir/v1", "design": {"name": "scale", "top": "scale", "source_format": "test"}, "instances": [], "ports": [], "clocks": [], "warnings": [],
             "nets": [{"id": f"n{i}", "drivers": [], "sinks": [], "cut_class": "undriven"} for i in range(count)]}
    EmuIR(value).save(source);del value
    measured = tmp_path/"paths.tsv"
    with measured.open("w") as stream:
        stream.write(STA_PATH_DATABASE_TSV_HEADER+"\n")
        for i in range(count):stream.write("\t".join([f"p{i}".encode().hex(), b"clk".hex(), "10", str(i%20-10), "0", f"n{i}".encode().hex()])+"\n")
    paths = tmp_path/"paths.json"
    imported = import_sta_path_database_tsv_streaming(measured, source, paths, provider="opensta-fpga-path-database-v1")
    proof = imported.validation
    target = tmp_path/"weights.json"
    tracemalloc.start()
    try:
        built = derive_partition_net_weights(paths, source, target, _validation=proof)
        checked = validate_partition_net_weights(target, paths, source)
        _current, peak = tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert built["timed_nets"] == checked["timed_nets"] == count
    assert peak < 5*1024*1024
