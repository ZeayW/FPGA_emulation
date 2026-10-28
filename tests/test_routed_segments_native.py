from tests.compile_adapter_fixture import preflight as run_preflight
"""Native routed graph equivalence and scale gates; not device qualification."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.physical.xilinx_netlist import emit_xilinx_mapped_index, IndexedXilinxMappedNetlist
from tests.routed_segment_entry import build_xilinx_segment_timing_bundle
from tests.xilinx_segment_timing_reference import (_TimingGraph,
    build_xilinx_boundary_timing, build_xilinx_logic_segment_timing, build_xilinx_local_path_timing)
from emuflow.frontend.yosys import import_yosys_json
from emuflow.timing.boundary_identity_storage import write_boundary_identity_storage
from tests.logic_identity_fixture import identity_facts
from tests.timing_facts import local_timing_facts
from emuflow.timing.local_path_timing import LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION


@pytest.fixture(autouse=True)
def native_runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def case(root):
    root.mkdir(parents=True, exist_ok=True)
    cells = {}
    for name, start, end in [("rx_lut", 3, 4), ("tx_lut", 5, 6), ("final", 6, 7)]:
        cells[name] = {"type": "LUT1", "parameters": {"INIT": "10"}, "attributes": {},
                       "port_directions": {"I0": "input", "O": "output"}, "connections": {"I0": [start], "O": [end]}}
    cells["shadow"] = {"type": "FDRE", "parameters": {"INIT": "0"}, "attributes": {},
                       "port_directions": {"C": "input", "D": "input", "Q": "output", "CE": "input", "R": "input"},
                       "connections": {"C": [2], "D": [4], "Q": [5], "CE": ["1"], "R": ["0"]}}
    source = root / "source.json"
    source.write_text(json.dumps({"creator": "test", "modules": {"dut": {"attributes": {"top": "1"},
        "ports": {"clk": {"direction": "input", "bits": [2]}, "rx": {"direction": "input", "bits": [3]},
                  "tx": {"direction": "output", "bits": [7]}}, "cells": cells,
        "netnames": {name: {"bits": [bit]} for name, bit in [("clk", 2), ("rx", 3), ("data", 4), ("state", 5), ("mid", 6), ("tx", 7)]}}}}))
    ir = root / "source.emuir.sqlite"
    import_yosys_json(source).save(ir)
    mapped = root / "mapped.sqlite"
    emit_xilinx_mapped_index(ir, mapped)
    endpoints = [{"sink": {"instance": name, "pin": pin}, "route_delay_ns": delay}
                 for name, pin, delay in [("rx_lut", "I0", .11), ("shadow", "D", .2), ("tx_lut", "I0", .3), ("final", "I0", .07)]]
    payload = root / "endpoints.jsonl"
    payload.write_bytes(b"".join(json.dumps(v, sort_keys=True).encode() + b"\n" for v in endpoints))
    timing = {"schema": "emuflow.xilinx-routed-timing/v2", "top": "dut",
              "qualification": {"logic_coefficients_ps": {"lut_a1": 70, "ff_clock_to_q": 25, "carry_co": 10}},
              "virtual_io": [{"logical_port": p, "logical_bit": 0, "direction": d, "route_delay_ns": n}
                             for p, d, n in [("rx", "rx", .11), ("tx", "tx", .4)]],
              "payloads": {"endpoints": {"format": "jsonl-object/v1", "path": payload.name,
                            "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(), "records": len(endpoints)}}}
    timing_path = root / "timing.json"
    timing_path.write_text(json.dumps(timing))
    from tests.test_boundary_identity_storage import _header, _record
    boundary = root / "boundary-id.json"
    records = [_record("tx", "tx", 0), _record("rx", "rx", 1)]
    for row in records:
        row["merged_ir"]["external_port"] = row["kind"]
        row["merged_ir"]["external_port_bit"] = 0
        row["merged_ir"]["boundary_register_instances"] = ["shadow"] if row["kind"] == "rx" else []
    with write_boundary_identity_storage(boundary, _header(), records):
        pass
    logic = root / "logic-id.json"
    logic_header = {"schema": "emuflow.logic-segment-identity/v1", "status": "pass", "design": "dut", "platform": "board",
                    "fpga": "fpga0", "provider": "fixture", "coverage": {}, "system_routes_sha256": "a"*64, "tdm_schedule_sha256": "b"*64}
    segments = [{"id": "s0", "kind": "launch", "system_path": "p0", "member_path": "m0", "cut_index": 0,
                 "fpga": "fpga0", "replace_tx_endpoint": "tx", "start_pin": "cell:shadow/Q", "end_pin": "top:tx"},
                {"id": "s1", "kind": "capture", "system_path": "p1", "member_path": "m1", "cut_index": 0,
                 "fpga": "fpga0", "replace_tx_endpoint": None, "start_pin": "top:rx", "end_pin": "cell:shadow/D"}]
    with identity_facts(logic, header=logic_header, segments=segments):
        pass
    from tests.test_local_path_timing_storage import _header as local_header, _record as local_record
    header = local_header()
    header["qualification"] = LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
    local = root / "local-id.json"
    local_rows = [{**local_record(name, 0.0), "start_pin": start, "end_pin": end,
                   "measurement": "endpoint-longest-path-fallback", "path_pins": []}
                  for name, start, end in [("a", "cell:shadow/Q", "top:tx"), ("b", "top:rx", "cell:shadow/D")]]
    with local_timing_facts(local, header, local_rows):
        pass
    return {"root": root, "ir": ir, "mapped": mapped, "timing_path": timing_path, "timing": timing,
            "payload": payload, "endpoints": endpoints, "boundary": boundary, "logic": logic, "local": local}


def run(c, *, logic=True, local=True):
    with IndexedXilinxMappedNetlist(c["mapped"]) as mapped:
        return build_xilinx_segment_timing_bundle(
            boundary_identity_path=c["boundary"], mapped_path=c["mapped"], timing_path=c["timing_path"],
            boundary_output_path=c["root"] / "boundary.json", mapped_value=mapped, timing_value=c["timing"],
            timing_validation={"status": "pass"}, source_ir_path=c["ir"],
            logic_identity_path=c["logic"] if logic else None, logic_output_path=c["root"] / "logic.json" if logic else None,
            local_identity_path=c["local"] if local else None, local_output_path=c["root"] / "local.json" if local else None,
        )


@pytest.mark.parametrize("logic,local", [(False, False), (True, False), (False, True), (True, True)])
def test_complete_typed_records_and_logical_seals_match_independent_graph(tmp_path, logic, local):
    c = case(tmp_path)
    with IndexedXilinxMappedNetlist(c["mapped"]) as mapped:
        reference_graph = _TimingGraph(mapped, c["timing"], c["endpoints"])
    references = {"boundary": build_xilinx_boundary_timing}
    if logic:
        references["logic"] = build_xilinx_logic_segment_timing
    if local:
        references["local"] = build_xilinx_local_path_timing
    for name, builder in references.items():
        builder(c[name], c["mapped"], c["timing_path"], tmp_path / (name + "-reference.json"), _timing_graph=reference_graph)
    with patch("tests.xilinx_segment_timing_reference._graph", side_effect=AssertionError("Python routed graph")), \
         patch("emuflow.physical.xilinx_netlist._IndexedCells.items", side_effect=AssertionError("Python cells")), \
         patch("emuflow.physical.xilinx_netlist._IndexedCells.values", side_effect=AssertionError("Python cells")):
        report = run(c, logic=logic, local=local)
    for name in references:
        module = importlib.import_module("emuflow.timing." + {"boundary": "boundary_timing", "logic": "logic_segment_timing", "local": "local_path_timing"}[name] + "_storage")
        opener = getattr(module, "read_" + {"boundary": "boundary_timing", "logic": "logic_segment_timing", "local": "local_path_timing"}[name] + "_storage")
        with opener(tmp_path / (name + ".json")) as actual, opener(tmp_path / (name + "-reference.json")) as expected:
            assert actual.selector["logical_sha256"] == expected.selector["logical_sha256"]
            key = {"boundary": "endpoints", "logic": "segments", "local": "paths"}[name]
            assert list(actual[key]) == list(expected[key])
    assert report["native_graph"]["implementation_id"] == "native-routed-segments-v1"
    assert report["native_graph"]["engine_metrics"]["wall_seconds"] >= 0
    assert report["native_graph"]["engine_metrics"]["maximum_process_rss_kib"] > 0


@pytest.mark.parametrize("feature", ["CARRY8", "MUXF7", "DSP48E2", "tie", "clock-gate", "memory-barrier"])
def test_primitive_offsets_barriers_clock_gates_and_tie_origins_match_reference(tmp_path, feature):
    c = case(tmp_path)
    with sqlite3.connect(c["mapped"]) as db:
        if feature in {"CARRY8", "MUXF7", "DSP48E2", "memory-barrier", "tie"}:
            cell_type = "RAMB18E2" if feature == "memory-barrier" else "LUT2" if feature == "tie" else feature
            record = json.loads(db.execute("SELECT record_json FROM cells WHERE id='final'").fetchone()[0])
            record["type"] = cell_type
            db.execute("UPDATE cells SET type=?,record_json=? WHERE id='final'", (cell_type, json.dumps(record)))
        if feature == "tie":
            seq = db.execute("SELECT seq FROM cells WHERE id='final'").fetchone()[0]
            bit = db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='rx_lut' AND port='I0'").fetchone()[0]
            db.execute("INSERT INTO cell_pins VALUES(?, 'final','I1',0,1,0,0,?,NULL)", (seq, bit))
    if feature == "tie":
        c["timing"]["qualification"]["logic_coefficients_ps"] = {"lut_a1": 0, "ff_clock_to_q": 0, "carry_co": 0}
        for row in c["endpoints"]:
            row["route_delay_ns"] = 0
        payload = b"".join(json.dumps(v).encode()+b"\n" for v in c["endpoints"])
        c["payload"].write_bytes(payload)
        c["timing"]["payloads"]["endpoints"]["sha256"] = hashlib.sha256(payload).hexdigest()
    if feature == "clock-gate":
        c["timing"]["clock_gates"] = [{"logical_cell": "rx_lut"}]
        from emuflow.timing.boundary_identity_storage import read_boundary_identity_storage
        with read_boundary_identity_storage(c["boundary"]) as view:
            header = dict(view.selector["header"])
            tx = [row for row in view["endpoints"] if row["kind"] == "tx"]
        with write_boundary_identity_storage(c["boundary"], header, tx):
            pass
    c["timing_path"].write_text(json.dumps(c["timing"]))
    with IndexedXilinxMappedNetlist(c["mapped"]) as mapped:
        reference = _TimingGraph(mapped, c["timing"], c["endpoints"])
    expected_path = tmp_path / "reference.json"
    build_xilinx_boundary_timing(c["boundary"], c["mapped"], c["timing_path"], expected_path, _timing_graph=reference)
    run(c, logic=False, local=False)
    assert read_json(tmp_path / "boundary.json")["logical_sha256"] == read_json(expected_path)["logical_sha256"]


@pytest.mark.parametrize("damage", ["digest", "count", "truncated", "negative", "driver", "unsupported", "unreachable", "cycle", "generation", "crc"])
def test_invalid_graph_or_request_preserves_all_previous_projections(tmp_path, damage):
    c = case(tmp_path)
    run(c)
    prior = {name: (tmp_path / (name + ".json")).read_bytes() for name in ("boundary", "logic", "local")}
    if damage in {"digest", "count"}:
        c["timing"]["payloads"]["endpoints"]["sha256" if damage == "digest" else "records"] = "0"*64 if damage == "digest" else 0
        c["timing_path"].write_text(json.dumps(c["timing"]))
    elif damage == "truncated":
        c["payload"].write_bytes(c["payload"].read_bytes()[:-1])
    elif damage == "negative":
        rows = c["endpoints"]
        rows[0]["route_delay_ns"] = -1
        c["payload"].write_bytes(b"".join(json.dumps(v).encode()+b"\n" for v in rows))
    elif damage in {"driver", "unsupported", "cycle"}:
        with sqlite3.connect(c["mapped"]) as db:
            if damage == "unsupported":
                db.execute("UPDATE cells SET type='unsupported' WHERE id='final'")
            elif damage == "driver":
                db.execute("UPDATE cell_pins SET direction=0 WHERE cell_id='shadow' AND port='Q'")
            else:
                # A driven feedback wire creates a reachable combinational cycle.
                bit = db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='final' AND port='O'").fetchone()[0]
                db.execute("UPDATE cell_pins SET net_bit=? WHERE cell_id='tx_lut' AND port='I0'", (bit,))
                row = db.execute("SELECT cell_seq,cell_id,port_bit,port_width,direction,value_kind,net_bit,constant FROM cell_pins WHERE cell_id='final' AND port='I0'").fetchone()
                source_bit = db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='shadow' AND port='Q'").fetchone()[0]
                db.execute("INSERT INTO cell_pins VALUES(?,?,'I1',?,?,?,?,?,?)", (*row[:2], *row[2:6], source_bit, row[7]))
    elif damage == "unreachable":
        value = read_json(c["logic"])
        with sqlite3.connect(tmp_path / value["artifact"]) as db:
            db.execute("UPDATE segments SET end_pin='unreachable'")
    from contextlib import ExitStack
    from tests import routed_segment_adapter as native
    with ExitStack() as stack:
        if damage == "crc":
            finish = native.CheckedRequestWriter.finish
            def corrupt(writer):
                path = Path(writer.stream.name)
                finish(writer)
                data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
            stack.enter_context(patch.object(native.CheckedRequestWriter, "finish", corrupt))
        elif damage == "generation":
            finish = native.NativeToolProcess.finish
            def change(process):
                result = finish(process)
                c["payload"].chmod(c["payload"].stat().st_mode ^ 0o100)
                return result
            stack.enter_context(patch.object(native.NativeToolProcess, "finish", change))
        with pytest.raises(ValidationError, match="cycle" if damage == "cycle" else None):
            run(c)
    for name, data in prior.items():
        assert (tmp_path / (name + ".json")).read_bytes() == data
    assert not list(tmp_path.glob(".routed-segments.*"))


def test_many_distinct_start_cones_do_not_initialize_or_scan_the_whole_graph_per_path(tmp_path):
    c = case(tmp_path)
    count = 20000
    with sqlite3.connect(c["mapped"]) as db:
        db.execute("DELETE FROM cell_pins"); db.execute("DELETE FROM cells")
        db.execute("UPDATE metadata SET value=? WHERE key='ports_json'", (json.dumps({"clk": {"direction": "input", "bits": [2]}}),))
        for i in range(count):
            for name, cell_type, seq in [(f"ff{i:05d}", "FDRE", 2*i), (f"lut{i:05d}", "LUT1", 2*i+1)]:
                db.execute("INSERT INTO cells VALUES(?,?,?,?)", (name, seq, cell_type, json.dumps({"type": cell_type})))
                pins = [("C", 0, 0, 2, None), ("D", 0, 1, None, "0"), ("Q", 1, 0, 20+2*i, None)] if cell_type == "FDRE" else [("I0", 0, 0, 20+2*i, None), ("O", 1, 0, 21+2*i, None)]
                for port, direction, kind, bit, constant in pins:
                    db.execute("INSERT INTO cell_pins VALUES(?,?,?,0,1,?,?,?,?)", (seq, name, port, direction, kind, bit, constant))
    c["endpoints"] = []
    c["payload"].write_bytes(b"")
    c["timing"]["virtual_io"] = []
    c["timing"]["payloads"]["endpoints"].update(records=0, sha256=hashlib.sha256(b"").hexdigest())
    c["timing_path"].write_text(json.dumps(c["timing"]))
    from tests.test_boundary_identity_storage import _header
    with write_boundary_identity_storage(c["boundary"], _header(), []):
        pass
    from tests.test_local_path_timing_storage import _header as local_header, _record
    header = local_header()
    header.update(qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION, coverage={"local_paths": count})
    header["source"]["original_paths"] = count
    def records():
        for i in range(count):
            yield {**_record(f"p{i:05d}", 0.0), "start_pin": f"cell:ff{i:05d}/Q", "end_pin": f"cell:lut{i:05d}/O",
                   "measurement": "endpoint-longest-path-fallback", "path_pins": []}
    with local_timing_facts(c["local"], header, records()):
        pass
    report = run(c, logic=False)
    graph = report["native_graph"]
    assert graph["solves"] == count
    assert graph["visited_nodes"] == 3*count
    assert graph["nodes"] > 4*count
    assert report["local_path"]["maximum_delay_ns"] == pytest.approx(.095)


def test_identity_selector_mutation_and_output_alias_are_rejected_before_publication(tmp_path):
    from tests import routed_segment_adapter as native
    c = case(tmp_path)
    run(c)
    prior = (tmp_path / "boundary.json").read_bytes()
    finish = native.NativeToolProcess.finish
    def change(process):
        result = finish(process)
        c["boundary"].chmod(c["boundary"].stat().st_mode ^ 0o100)
        return result
    with patch.object(native.NativeToolProcess, "finish", change), pytest.raises(ValidationError, match="generation changed"):
        run(c)
    assert (tmp_path / "boundary.json").read_bytes() == prior
    with IndexedXilinxMappedNetlist(c["mapped"]) as mapped, pytest.raises(ValidationError, match="aliases a source"):
        build_xilinx_segment_timing_bundle(boundary_identity_path=c["boundary"], mapped_path=c["mapped"],
            timing_path=c["timing_path"], boundary_output_path=c["boundary"], mapped_value=mapped,
            timing_value=c["timing"], timing_validation={}, source_ir_path=c["ir"])


@pytest.mark.parametrize("source_has_driver", [False, True])
def test_explicit_floating_input_stays_disconnected_without_becoming_a_source(tmp_path, source_has_driver):
    c = case(tmp_path)
    with sqlite3.connect(c["mapped"]) as db:
        bit = db.execute("SELECT net_bit FROM cell_pins WHERE cell_id='shadow' AND port='D'").fetchone()[0]
        name = db.execute("SELECT id FROM netnames WHERE seq=?", (bit-2,)).fetchone()[0]
        db.execute("DELETE FROM cell_pins WHERE cell_id='rx_lut' AND port='O'")
    with sqlite3.connect(c["ir"]) as db:
        db.execute("UPDATE net_records SET cut_class='undriven' WHERE id=?", (name,))
        if not source_has_driver:
            db.execute("DELETE FROM endpoint_records WHERE net_seq=? AND role=0", (bit-2,))
    from tests.test_boundary_identity_storage import _header
    with write_boundary_identity_storage(c["boundary"], _header(), []):
        pass
    from tests.test_local_path_timing_storage import _header as local_header, _record
    header = local_header()
    header["qualification"] = LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
    header["coverage"] = {"local_paths": 1}
    row = {**_record("a", 0.0), "start_pin": "cell:shadow/Q", "end_pin": "top:tx",
           "measurement": "endpoint-longest-path-fallback", "path_pins": []}
    with local_timing_facts(c["local"], header, [row]):
        pass
    if source_has_driver:
        with pytest.raises(ValidationError, match="undriven source has a driver"):
            run(c, logic=False)
    else:
        report = run(c, logic=False)
        assert report["source_constraints"]["undriven_bit_count"] == 1
        assert "undriven_bits" not in report["source_constraints"]
        prior = (tmp_path / "local.json").read_bytes()
        row.update(start_pin="top:rx", end_pin="cell:shadow/D")
        with local_timing_facts(c["local"], header, [row]):
            pass
        with pytest.raises(ValidationError, match="no path"):
            run(c, logic=False)
        assert (tmp_path / "local.json").read_bytes() == prior


def test_native_routed_graph_capability_is_required_before_synthesis(tmp_path):
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    def capability(schema):
        if schema == "routed-segment-request-v1":
            raise EmuFlowError("missing native routed graph")
    with patch("emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract"), patch(
        "emuflow.common.native_tools.require_native_schema", side_effect=capability
    ), patch("emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend started")), \
         pytest.raises(EmuFlowError, match="missing native routed graph"):
        run_preflight(sources=[tmp_path / "missing.v"], top="dut", clocks=["clk"], clock_periods={"clk": 10.0},
                           physical=True, physical_backend="rapidwright", output_dir=tmp_path / "flow")
    assert not (tmp_path / "flow").exists()


@pytest.mark.parametrize("field,value", [("design", "other-design"), ("fpga", "fpga1")])
def test_mixed_projection_namespace_cannot_publish(tmp_path, field, value):
    from emuflow.timing.local_path_timing_storage import read_local_path_timing_storage
    c = case(tmp_path)
    run(c)
    previous = {name: (tmp_path / (name + ".json")).read_bytes() for name in ("boundary", "logic", "local")}
    with read_local_path_timing_storage(c["local"]) as view:
        header, records = dict(view.selector["header"]), list(view["paths"])
    header[field] = value
    if field == "fpga":
        for row in records:
            row["fpga"] = value
    with local_timing_facts(c["local"], header, records):
        pass
    with pytest.raises(ValidationError, match="namespaces disagree"):
        run(c)
    for name, data in previous.items():
        assert (tmp_path / (name + ".json")).read_bytes() == data
