from tests.compile_adapter_fixture import preflight as run_preflight
from tests.physical_handoff_fixture import write_handoff_fixture
"""Source-bound endpoint producer equivalence; not physical qualification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.common.native_tools import native_file_generation
from emuflow.physical.xilinx_netlist import IndexedXilinxMappedNetlist
from emuflow.physical.xilinx_physical_handoff import open_xilinx_physical_handoff
from emuflow.physical.rapidwright.xilinx_rwroute import validate_xilinx_route_db
from tests.routed_timing_adapter import build_xilinx_routed_timing, validate_xilinx_routed_timing
from tests.test_rwroute_native import write_mapped
from tests import test_xilinx_rwroute as rwroute_fixture
from tests.xilinx_timing_reference import build_xilinx_routed_timing as reference


@pytest.fixture(autouse=True)
def native_runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def case(root):
    root.mkdir(parents=True, exist_ok=True)
    mapped = {"modules": {"top": {"ports": {}, "cells": {
        "src": {"type": "LUT1", "parameters": {}, "port_directions": {"O": "output"}, "connections": {"O": [2]}},
        "sink_a": {"type": "FDRE", "parameters": {}, "port_directions": {"D": "input"}, "connections": {"D": [2]}},
        "sink_b": {"type": "FDRE", "parameters": {}, "port_directions": {"D": "input"}, "connections": {"D": [2]}}}}}}
    packed = {"schema": "emuflow.packed-site-netlist/v1", "status": "pass", "mapping_profile": "fixture", "top": "top",
              "source": {}, "policy": {}, "clusters": [], "cascade_chains": [], "unplaced_constants": [], "summary": {}}
    placement = {"schema": "emuflow.xilinx-placement/v1", "status": "pass", "part": "xcvu19p-test", "provider": "fixture",
                 "source": {}, "policy": {}, "clusters": [], "summary": {}}
    for i, (name, cell) in enumerate(mapped["modules"]["top"]["cells"].items()):
        assignment = {"instance": name, "cell_type": cell["type"], "bel": "A6LUT" if i == 0 else "AFF"}
        packed["clusters"].append({"id": name, "kind": "slice", "site_templates": ["SLICEL"], "control_set": None, "assignments": [assignment]})
        placement["clusters"].append({"cluster": name, "site": f"S{i}", "site_type": "SLICEL", "x": 0, "y": i, "fixed": False,
                                      "assignments": [{**assignment, "placement_mode": "fixed", "site": f"S{i}"}]})
    mapped_path, physical = root / "mapped.sqlite", root / "physical.sqlite"
    write_mapped(mapped_path, mapped)
    write_handoff_fixture(physical, packed, placement)
    route = rwroute_fixture.XilinxRWRouteTest()._route()
    route["nets"][0]["net"] = "n2"
    digests = {"mapped_sha256": file_sha256(mapped_path), "packed_sha256": file_sha256(physical), "placement_sha256": file_sha256(physical)}
    route["source"].update(digests)
    route_path, nets, pips = rwroute_fixture.XilinxRWRouteTest()._stream_route(root, route)
    route = read_json(route_path)
    with IndexedXilinxMappedNetlist(mapped_path) as view:
        p, q = open_xilinx_physical_handoff(physical, validated_sha256=digests["packed_sha256"])
        validation = validate_xilinx_route_db(route_path, mapped_path=mapped_path, packed_path=physical, placement_path=physical,
                                             mapped_value=view, packed_value=p, source_sha256=digests)
    return dict(root=root, mapped=mapped, packed=packed, placement=placement, mapped_path=mapped_path, physical=physical,
                route_path=route_path, nets=nets, pips=pips, route=route, validation=validation, digests=digests)


def run(c, output=None, sink=None, *, view=None, physical_views=None):
    output = output or c["root"] / "native" / "timing.json"
    with IndexedXilinxMappedNetlist(c["mapped_path"]) as opened:
        packed, placement = physical_views or open_xilinx_physical_handoff(c["physical"], validated_sha256=c["digests"]["packed_sha256"])
        return build_xilinx_routed_timing(c["mapped_path"], c["physical"], c["physical"], c["route_path"], output,
            mapped_value=view or opened, packed_value=packed, placement_value=placement,
            route_value=c["route"], source_sha256=c["digests"], route_validation=c["validation"], timing_value_sink=sink)


def test_native_records_manifest_bytes_and_validation_match_independent_producer(tmp_path):
    c = case(tmp_path)
    expected, actual = tmp_path / "reference" / "timing.json", tmp_path / "native" / "timing.json"
    reference(c["mapped_path"], c["physical"], c["physical"], c["route_path"], expected,
              mapped_value=c["mapped"], packed_value=c["packed"], placement_value=c["placement"], route_value=c["route"],
              source_sha256=c["digests"], route_validation=c["validation"])
    sink = {}
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("Python route metadata population")
    c["route"]["clock_gates"] = NoIteration()
    c["route"]["virtual_io"] = NoIteration()
    with patch("emuflow.physical.xilinx_netlist._IndexedCells.items", side_effect=AssertionError("Python mapped cells")), \
         patch("emuflow.physical.xilinx_physical_handoff._ClusterSequence.__iter__", side_effect=AssertionError("Python sites")), \
         patch("tests.xilinx_timing_reference._build_payload", side_effect=AssertionError("Python endpoints")):
        report = run(c, actual, sink)
    assert actual.read_bytes() == expected.read_bytes()
    assert actual.with_name(actual.name + ".endpoints.jsonl").read_bytes() == expected.with_name(expected.name + ".endpoints.jsonl").read_bytes()
    assert report["timing_sha256"] == file_sha256(actual)
    assert report["native_metrics"]["wall_seconds"] >= 0
    assert sink["summary"]["logical_endpoints"] == 2
    assert not isinstance(sink["virtual_io"], list)
    validate_xilinx_routed_timing(actual, _value=sink)


@pytest.mark.parametrize("damage", ["count", "digest", "truncated", "duplicate-bit", "negative", "unbound-site", "driver-site", "missing-sink", "owner", "crc", "generation"])
def test_rejects_bad_input_without_replacing_manifest_or_endpoint_owner(tmp_path, damage):
    from tests import routed_endpoint_adapter as native
    c = case(tmp_path)
    output = tmp_path / "native" / "timing.json"
    run(c, output)
    prior = {p: p.read_bytes() for p in (output, output.with_name(output.name + ".endpoints.jsonl"))}
    if damage in {"count", "digest"}:
        c["route"]["payloads"]["nets"]["records" if damage == "count" else "sha256"] = 0 if damage == "count" else "0"*64
        c["route_path"].write_text(json.dumps(c["route"]))
    elif damage in {"duplicate-bit", "negative", "unbound-site", "driver-site", "missing-sink"}:
        rows = [json.loads(line) for line in c["nets"].read_text().splitlines()]
        if damage == "duplicate-bit":
            rows *= 2
        elif damage == "negative":
            rows[0]["pins"][1]["route_delay_ps"] = -1
        elif damage == "unbound-site":
            rows[0]["pins"][1]["site"] = "foreign"
        elif damage == "driver-site":
            rows[0]["pins"][0]["site"] = "foreign"
        else:
            rows[0]["pins"] = rows[0]["pins"][:-1]
        c["nets"].write_bytes(b"".join(json.dumps(row).encode()+b"\n" for row in rows))
        c["route"]["payloads"]["nets"].update(records=len(rows), sha256=file_sha256(c["nets"]))
        c["route_path"].write_text(json.dumps(c["route"]))
    elif damage == "truncated":
        c["nets"].write_bytes(c["nets"].read_bytes()[:-1])
    elif damage == "owner":
        with sqlite3.connect(c["physical"]) as db:
            db.execute("UPDATE assignment SET instance='foreign' WHERE instance='sink_a'")
    # Hand a current file generation to the parser corruption cases; the
    # independent producer must reject semantics/digest, not merely stale stat.
    if damage not in {"crc", "generation"}:
        c["validation"]["source_generations"] = {name: list(native_file_generation(path))
            for name, path in [("route", c["route_path"]), ("nets", c["nets"]), ("pips", c["pips"])]}
    from contextlib import ExitStack
    with ExitStack() as stack:
        if damage == "crc":
            finish = native.CheckedRequestWriter.finish
            def corrupt(writer):
                path = Path(writer.stream.name);finish(writer)
                data = bytearray(path.read_bytes());data[-1] ^= 1;path.write_bytes(data)
            stack.enter_context(patch.object(native.CheckedRequestWriter, "finish", corrupt))
        if damage == "generation":
            finish = native.NativeToolProcess.finish
            def mutate(process):
                result = finish(process)
                c["nets"].chmod(c["nets"].stat().st_mode ^ 0o100)
                return result
            stack.enter_context(patch.object(native.NativeToolProcess, "finish", mutate))
        with pytest.raises(ValidationError):
            run(c, output)
    for p, contents in prior.items():
        assert p.read_bytes() == contents
    assert not list(output.parent.glob(".routed-endpoints.*"))


def test_open_physical_generation_cannot_change_before_endpoint_binding(tmp_path):
    c = case(tmp_path)
    views = open_xilinx_physical_handoff(c["physical"], validated_sha256=c["digests"]["packed_sha256"])
    c["physical"].chmod(c["physical"].stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match="generation changed"):
        run(c, physical_views=views)


def test_path_producer_keeps_generation_from_before_manifest_read(tmp_path):
    c = case(tmp_path)
    output = tmp_path / "native" / "timing.json"
    def mutate_after_read(path):
        value = read_json(path)
        path.chmod(path.stat().st_mode ^ 0o100)
        return value
    with patch("tests.routed_timing_adapter.read_json", side_effect=mutate_after_read), \
         pytest.raises(ValidationError, match="generation changed"):
        build_xilinx_routed_timing(c["mapped_path"], c["physical"], c["physical"], c["route_path"], output)
    assert not output.exists()


def test_consumer_keeps_the_produced_timing_generation(tmp_path):
    from tests.routed_endpoint_adapter import bind_routed_timing_sources
    from emuflow.timing.xilinx_opensta import run_xilinx_routed_opensta
    c = case(tmp_path);sink = {};output = tmp_path / "native" / "timing.json"
    run(c, output, sink)
    sources=bind_routed_timing_sources(output,sink)
    output.chmod(output.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match="generation changed"):
        bind_routed_timing_sources(output, sink)
    with pytest.raises(ValidationError, match="generation changed"):
        run_xilinx_routed_opensta(c["mapped_path"],output,tmp_path/'paths.json',tmp_path/'summary.json',
            clocks={'clk':10.0},executable='/usr/bin/false',timing_sources=sources)


def test_endpoint_consumer_does_not_reopen_or_retain_qualified_pip_payload(tmp_path):
    c = case(tmp_path)
    # PIPs were independently checked by case(). Endpoint timing consumes the
    # sealed route facts and net stream, so the last reader may retire PIPs.
    c["pips"].unlink()
    assert run(c)["logical_endpoints"] == 2


def _rebind_parser_fixture(c, nets):
    """Freeze a semantic unit input, without claiming physical qualification."""
    c["digests"] = {"mapped_sha256": file_sha256(c["mapped_path"]), "packed_sha256": file_sha256(c["physical"]),
                    "placement_sha256": file_sha256(c["physical"])}
    c["route"]["source"].update(c["digests"])
    c["nets"].write_bytes(b"".join(json.dumps(row, sort_keys=True).encode()+b"\n" for row in nets))
    c["route"]["payloads"]["nets"].update(records=len(nets), sha256=file_sha256(c["nets"]))
    c["route_path"].write_text(json.dumps(c["route"]))
    c["validation"].update(route_sha256=file_sha256(c["route_path"]), source_sha256=c["digests"],
        source_generations={name: list(native_file_generation(path)) for name, path in
                            [("route", c["route_path"]), ("nets", c["nets"]), ("pips", c["pips"])]})


@pytest.mark.parametrize("feature", ["shared-site", "intra-site", "static", "rx", "tx", "clock-gate"])
def test_binding_modes_and_metadata_match_independent_record_producer(tmp_path, feature):
    c = case(tmp_path)
    nets = [json.loads(line) for line in c["nets"].read_text().splitlines()]
    if feature in {"shared-site", "intra-site"}:
        target = "S1" if feature == "shared-site" else "S0"
        moved = "sink_b" if feature == "shared-site" else "sink_a"
        destination = "sink_a" if feature == "shared-site" else "src"
        old = next(row for row in c["packed"]["clusters"] if row["id"] == moved)
        kept = next(row for row in c["packed"]["clusters"] if row["id"] == destination)
        kept["assignments"].extend([{**old["assignments"][0], "bel": "BFF"}])
        c["packed"]["clusters"].remove(old)
        old = next(row for row in c["placement"]["clusters"] if row["cluster"] == moved)
        kept = next(row for row in c["placement"]["clusters"] if row["cluster"] == destination)
        kept["assignments"].extend([{**old["assignments"][0], "site": target, "bel": "BFF"}])
        c["placement"]["clusters"].remove(old)
        write_handoff_fixture(c["physical"], c["packed"], c["placement"])
        if feature == "shared-site":
            nets[0]["pins"][2]["site"] = target
        else:
            nets[0]["pins"].pop(1)
    elif feature == "static":
        nets.append({"net": "GLOBAL_LOGIC0", "kind": "static_gnd", "qualification": "device-tied-static", "pip_count": 0,
                     "pins": [{"site": "S1", "is_output": False}]})
    elif feature == "rx":
        with sqlite3.connect(c["mapped_path"]) as db:
            db.execute("DELETE FROM cell_pins WHERE cell_id='src'")
        c["mapped"]["modules"]["top"]["cells"]["src"]["connections"] = {}
        c["mapped"]["modules"]["top"]["cells"]["src"]["port_directions"] = {}
        nets[0]["pins"][0]["site"] = "IOB_P"
        c["route"]["virtual_io"] = [{"id": "io0", "direction": "rx", "mapped_net": "n2", "logical_port": "input", "logical_bit": 0,
                                     "p_site": "IOB_P", "n_site": "IOB_N", "route_delay_ps": 100.0}]
    elif feature == "tx":
        with sqlite3.connect(c["mapped_path"]) as db:
            db.execute("DELETE FROM cell_pins WHERE cell_id!='src'")
        for name in ("sink_a", "sink_b"):
            c["mapped"]["modules"]["top"]["cells"][name].update(connections={}, port_directions={})
        nets[0]["pins"] = [nets[0]["pins"][0], {"site": "IOB_P", "is_output": False, "route_delay_ps": 100.0}]
        c["route"]["virtual_io"] = [{"id": "io0", "direction": "tx", "mapped_net": "n2", "logical_port": "output", "logical_bit": 0,
                                     "p_site": "IOB_P", "n_site": "IOB_N", "route_delay_ps": 100.0}]
    else:
        c["route"]["clock_gates"] = [{"logical_cell": "src", "physical_site": "BUFG", "physical_type": "BUFGCE",
                                      "qualification": "lut2-init-1000-fdre-latched-enable-v1", "cascaded_input": True, "input_site": "S0",
                                      "implementation": "lut2-prefilter-bufgce-tied-high"}]
        nets[0]["pins"][0]["site"] = "BUFG"
    _rebind_parser_fixture(c, nets)
    expected, actual = tmp_path / "reference" / "timing.json", tmp_path / "native" / "timing.json"
    reference(c["mapped_path"], c["physical"], c["physical"], c["route_path"], expected,
              mapped_value=c["mapped"], packed_value=c["packed"], placement_value=c["placement"], route_value=c["route"],
              source_sha256=c["digests"], route_validation=c["validation"])
    sink = {}
    run(c, actual, sink)
    assert actual.read_bytes() == expected.read_bytes()
    assert actual.with_name(actual.name+".endpoints.jsonl").read_bytes() == expected.with_name(expected.name+".endpoints.jsonl").read_bytes()
    assert list(sink["clock_gates"]) == read_json(actual)["clock_gates"]
    assert list(sink["virtual_io"]) == read_json(actual)["virtual_io"]


def test_manifest_array_reader_streams_large_array_and_detects_mutation(tmp_path):
    from tests.routed_endpoint_adapter import _TimingMetadataArray
    from emuflow.storage.store_publication import bind_store_source
    manifest, payload = tmp_path / "manifest.json", tmp_path / "payload.jsonl"
    count = 100000
    with manifest.open("w") as out:
        out.write('{"clock_gates":[],"virtual_io":[')
        for i in range(count):
            if i:
                out.write(',')
            out.write(json.dumps({"id": f"io{i:06d}"}))
        out.write(']}')
    payload.write_bytes(b'')
    sources = (bind_store_source(manifest, "xilinx-routed-timing-v2"), bind_store_source(payload, "xilinx-routed-endpoints-v1"))
    view = _TimingMetadataArray("virtual_io", count, sources)
    with patch("json.load", side_effect=AssertionError("whole manifest loaded")), patch("json.loads", side_effect=AssertionError("whole manifest loaded")):
        assert sum(1 for _ in view) == count
    assert view[-1] == {"id": "io099999"}
    manifest.chmod(manifest.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match="generation changed"):
        next(iter(view))


@pytest.mark.parametrize("after_manifest", [False, True])
def test_group_publication_failure_restores_both_old_outputs(tmp_path, after_manifest):
    from emuflow.storage import store_publication as publisher
    c = case(tmp_path);output = tmp_path / "native" / "timing.json"
    run(c, output)
    endpoints = output.with_name(output.name+".endpoints.jsonl")
    prior = {p: p.read_bytes() for p in (output, endpoints)}
    nets = [json.loads(line) for line in c["nets"].read_text().splitlines()]
    nets[0]["pins"][1]["route_delay_ps"] += 1
    _rebind_parser_fixture(c, nets)
    replace = publisher.os.replace
    injected = False
    def fail(source, target):
        nonlocal injected
        if Path(target) == output and not injected:
            injected = True
            if after_manifest:
                replace(source, target)
            raise OSError("injected manifest publication failure")
        return replace(source, target)
    with patch.object(publisher.os, "replace", side_effect=fail), pytest.raises(OSError, match="injected manifest"):
        run(c, output)
    for path, content in prior.items():
        assert path.read_bytes() == content


def test_large_repeated_net_population_uses_no_python_site_or_endpoint_table(tmp_path):
    c = case(tmp_path)
    count = 20000
    with sqlite3.connect(c["mapped_path"]) as mapped, sqlite3.connect(c["physical"]) as physical:
        mapped.row_factory = physical.row_factory = sqlite3.Row
        cell_rows = [dict(row) for row in mapped.execute("SELECT * FROM cells ORDER BY seq")]
        pin_rows = {cell["id"]: [dict(row) for row in mapped.execute("SELECT * FROM cell_pins WHERE cell_id=?", (cell["id"],))] for cell in cell_rows}
        clusters = {row["ordinal"]: dict(row) for row in physical.execute("SELECT * FROM cluster")}
        assignments = {row["instance"]: dict(row) for row in physical.execute("SELECT * FROM assignment")}
        mapped.execute("DELETE FROM cell_pins"); mapped.execute("DELETE FROM cells")
        physical.execute("DELETE FROM assignment"); physical.execute("DELETE FROM cluster"); physical.execute("DROP TABLE IF EXISTS feature")
        def insert(db, table, row):
            db.execute("INSERT INTO " + table + "(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        for i in range(count):
            for j, old_cell in enumerate(cell_rows):
                name, seq = f"{old_cell['id']}-{i:05d}", 3*i+j
                insert(mapped, "cells", {**old_cell, "id": name, "seq": seq})
                for pin in pin_rows[old_cell["id"]]:
                    insert(mapped, "cell_pins", {**pin, "cell_id": name, "cell_seq": seq, "net_bit": i+2})
                assignment = assignments[old_cell["id"]]
                site = "S-" + name
                insert(physical, "cluster", {**clusters[assignment["cluster_ordinal"]], "ordinal": seq,
                                              "cluster_id": name, "site": site, "y": seq})
                insert(physical, "assignment", {**assignment, "cluster_ordinal": seq, "instance": name, "physical_site": site})
        physical.execute("UPDATE manifest SET packed_clusters=?,assignments=?", (3*count, 3*count))
    c["digests"] = {"mapped_sha256": file_sha256(c["mapped_path"]), "packed_sha256": file_sha256(c["physical"]),
                    "placement_sha256": file_sha256(c["physical"])}
    c["route"]["source"].update(c["digests"])
    with c["nets"].open("w") as out:
        for i in range(count):
            out.write(json.dumps({"net": f"n{i+2}", "kind": "signal", "pip_count": 0, "pins": [
                {"site": f"S-src-{i:05d}", "is_output": True},
                {"site": f"S-sink_a-{i:05d}", "is_output": False, "route_delay_ps": 10.0},
                {"site": f"S-sink_b-{i:05d}", "is_output": False, "route_delay_ps": 20.0}]}) + "\n")
    c["route"]["payloads"]["nets"].update(records=count, sha256=file_sha256(c["nets"]))
    c["route"]["cells"] = 3*count
    c["route_path"].write_text(json.dumps(c["route"]))
    c["validation"].update(route_sha256=file_sha256(c["route_path"]), source_sha256=c["digests"],
        source_generations={name: list(native_file_generation(path)) for name, path in
                            [("route", c["route_path"]), ("nets", c["nets"]), ("pips", c["pips"])]})
    # This is a repeated binding-kernel input, not a qualified physical route.
    with patch("emuflow.physical.xilinx_netlist._IndexedCells.items", side_effect=AssertionError("Python cells")), \
         patch("emuflow.physical.xilinx_physical_handoff._ClusterSequence.__iter__", side_effect=AssertionError("Python sites")):
        report = run(c)
    assert report["logical_endpoints"] == 2*count
    assert report["exact_site_bindings"] == 2*count
    assert report["maximum_route_delay_ns"] == .02
    from tests.routed_timing_adapter import iter_xilinx_routed_timing_endpoints
    output = tmp_path / "native" / "timing.json"
    assert sum(1 for _ in iter_xilinx_routed_timing_endpoints(output, read_json(output))) == 2*count


def test_array_slice_uses_one_cursor_and_closes_it(tmp_path):
    from tests.routed_endpoint_adapter import _TimingMetadataArray
    from emuflow.storage.store_publication import bind_store_source
    manifest, payload = tmp_path / "manifest.json", tmp_path / "payload.jsonl"
    manifest.write_text(json.dumps({"virtual_io": list(range(100))}))
    payload.write_bytes(b"")
    view = _TimingMetadataArray("virtual_io", 100, (bind_store_source(manifest, "xilinx-routed-timing-v2"),
                                                  bind_store_source(payload, "xilinx-routed-endpoints-v1")))
    original = _TimingMetadataArray.__iter__
    traversals = []
    def observe(self):
        traversals.append(True)
        yield from original(self)
    with patch.object(_TimingMetadataArray, "__iter__", observe):
        assert view[5:80:3] == list(range(100))[5:80:3]
        assert len(traversals) == 1
        assert view[90:15:-4] == list(range(100))[90:15:-4]
        assert len(traversals) == 2


def test_native_endpoint_capability_fails_before_frontend(tmp_path):
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    def require(schema):
        if schema == "routed-endpoint-request-v1":
            raise EmuFlowError("missing native endpoint producer")
    with patch("emuflow.flow.multi_fpga_flow._validate_mapping_timing_contract"), \
         patch("emuflow.common.native_tools.require_native_schema", side_effect=require), \
         patch("emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend started")), \
         pytest.raises(EmuFlowError, match="missing native endpoint producer"):
        run_preflight(sources=[tmp_path / "absent.v"], top="dut", clocks=["clk"], clock_periods={"clk": 10.0},
                           physical=True, physical_backend="rapidwright", output_dir=tmp_path / "flow")
    assert not (tmp_path / "flow").exists()


def test_retired_fragment_timing_commands_are_not_exposed(tmp_path):
    from emuflow.flow.cli import main
    for command in ('build-xilinx-routed-timing','validate-xilinx-routed-timing'):
        with pytest.raises(SystemExit):
            main(['arch',command])
