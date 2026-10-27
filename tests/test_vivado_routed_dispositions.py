"""Routed source closure explains removals without constraining optimization."""
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json
from emuflow.vivado_session import SCHEMA, _tcl_word, prepare_timing
from emuflow.vivado_physical import _import_routed_measurement
from tests.test_vivado_optimization import VivadoOptimizationTests
from tests.test_vivado_session import ROOT, TCL86, session_fixture


@pytest.fixture
def unobservable_session(tmp_path):
    model = tmp_path/"model"; model.mkdir()
    mapped, _, digest, origins = VivadoOptimizationTests().prepare(model, dynamic_dead=True)
    out = tmp_path/"native"; out.mkdir()
    shutil.copyfile(model/"inventory.tsv", out/"routed_mapped_cells.tsv")
    queries = {}
    for kind in ["local_path_timing", "logic_segment_timing", "boundary_timing"]:
        path = tmp_path/f"{kind}.query.tsv"
        if kind == "boundary_timing":
            header = ["endpoint_hex", "kind", "external_port_hex", "bit", "logical_net_hex", "boundary_cell_hex"]
            row = [b"boundary".hex(), "rx", b"dead_rx".hex(), "0", b"n5".hex(), b"dead".hex()]
        else:
            header = ["endpoint_hex", "kind", "start_kind", "start_object_hex", "end_kind", "end_object_hex", "cone_anchor_kind", "cone_anchor_object_hex"]
            row = [b"local".hex(), "local", "port", b"d".hex(), "pin", b"dead/D".hex(), "", ""]
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerows([header, row])
        queries[kind] = str(path)
    session = dict(schema=SCHEMA, fpga="fpga0", output=str(out), mapped_index=str(mapped),
        functions=str(mapped.with_name("dut.functions.sqlite")), mapped_index_sha256=digest,
        timing_query_mode="clock-aware-reference", queries=queries)
    return session, origins


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
def test_default_routed_export_retains_removed_dynamic_paths_without_measuring_zero(unobservable_session):
    session, origins = unobservable_session
    out = Path(session["output"])
    inputs = [Path(session["mapped_index"]), out/"routed_mapped_cells.tsv", *map(Path, session["queries"].values())]
    parents = {path:file_sha256(path) for path in inputs}
    result = prepare_timing(session)
    assert result["timing_coverage"] == {"status":"complete", "requested_queries":3,
        "measurable_queries":0, "optimized_away_queries":3, "unresolved_queries":0}
    preparation = read_json(out/"routed-query-preparation.json")
    assert preparation["optimization_proofs"]["constant_hypotheses"] == 0
    assert preparation["rx_shadow_scope"]["status"] == "complete"
    assert preparation["rx_shadow_scope"]["native_cells"] == 0
    assert preparation["rx_shadow_scope"]["optimized_away_queries"] == 1
    program = f"source {_tcl_word(ROOT/'scripts/vivado/native_session.tcl')}\n"
    program += f"set emuflow_session_out {_tcl_word(out)}\nset emuflow_timing_query_mode clock-aware-reference\n"
    program += r'''
proc current_design {args} {return dut}
proc get_clocks {args} {return {}}
proc get_pins {args} {error "unobservable path resolved to physical pins"}
proc get_timing_paths {args} {error "unobservable path assigned a numerical delay"}
proc emuflow_session_child {args} {error "query preparation repeated"}
'''
    program += f"emuflow_session_timings {_tcl_word(ROOT/'scripts/vivado')} partition {{}} false true\n"
    completed = subprocess.run([TCL86], input=program, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    assert not completed.stderr
    for kind in session["queries"]:
        with (out/f"{kind}.tsv").open() as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        assert len(rows) == 1
        assert rows[0]["measurement"] == f"optimized-away:s{origins['dead']}"
        for column in ["delay_ns", "reference_cost_ns", "check_clock_adjustment_ns"]:
            assert rows[0][column] == ""
    assert parents == {path:file_sha256(path) for path in parents}
    from emuflow.vivado_physical_timing import import_vivado_local_timing
    from emuflow.local_path_timing_storage import read_local_path_timing_storage
    from emuflow.local_path_timing import path_id_set_sha256
    from tests.test_local_path_timing_storage import _header, _record
    header = _header(); header["coverage"]["local_paths"] = 1
    header["source"].update(original_paths=1, original_path_ids_sha256=path_id_set_sha256(["local"]))
    record = {**_record("local", 1.), "start_pin":"d", "end_pin":"dead/D",
        "measurement":"endpoint-longest-path-fallback", "path_pins":[]}
    record.pop("delay_ns")
    identity = out/"identity.sqlite"
    with sqlite3.connect(identity) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("CREATE TABLE paths(id TEXT PRIMARY KEY,record_json TEXT)")
        db.execute("INSERT INTO metadata VALUES('header',?)", (json.dumps({**header,"schema":"emuflow.local-path-identity/v2"}),))
        db.execute("INSERT INTO paths VALUES('local',?)", (json.dumps(record),))
    stage = _import_routed_measurement(kind="local_path_timing", output=out, identity=identity,
        resolution=preparation["resolutions"]["local_path_timing"], importer=import_vivado_local_timing,
        export={"status":"pass"}, source_dispositions=preparation["optimization_proofs"])
    assert stage["import"]["optimized_away_paths"] == 1
    with read_local_path_timing_storage(out/"local_path_timing.json") as view:
        assert view["paths"][0]["delay_ns"] is None
        assert view["paths"][0]["optimization_source"] == f"s{origins['dead']}"


def test_unavailable_disposition_analysis_does_not_prevent_routed_measurements(tmp_path, monkeypatch):
    session, _, _ = session_fixture(tmp_path)
    def unavailable(*args, **kwargs):
        raise ValidationError("source closure analysis unavailable")
    monkeypatch.setattr("emuflow.vivado_session.build_optimization_proofs", unavailable)
    assert prepare_timing(session)["status"] == "pass"
    result = read_json(Path(session["output"])/"routed-query-preparation.json")
    assert result["source_disposition_analysis"] == {"status":"incomplete", "error":"source closure analysis unavailable"}
    assert result["timing_coverage"]["unresolved_queries"] == 1
    assert "optimization_proofs" not in result
