"""Differential tests of the production native ScheduleStore owner."""
import json
import sqlite3
import struct
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.tdm_schedule_storage import (
    FULLY_TYPED_DDL, TYPED_INDEX_DDL, read_tdm_schedule,
    tdm_schedule_store_path, write_tdm_schedule_storage,
)
from tests.native_build import emuflow_native


class NoTraversal(list):
    def __iter__(self):
        raise AssertionError("native publication traversed Python records")


def _native_source(tmp_path, empty=False):
    binary = emuflow_native()
    input_path, result_path = (tmp_path / name for name in ("input.bin", "result.bin"))
    fpga, links, capacities = ["a", "b", "c", "d"], ["ab", "ac", "bc", "bd"], ["c0", "c1", "c2", "c3"]
    count = 0 if empty else 2
    missing = 2**32 - 1
    routes = [
        ("d9-é\n\"😀\x7f", "n0-汉字", 0, [3, 2], 1,
         [(0,0,0,0,1,0,4,missing,1,1), (0,1,1,0,2,0,4,missing,1,1), (1,3,3,1,3,0,4,missing,1,1)], [2,0,1]),
        ("d0", "n1", 1, [2], 0, [(0,2,2,1,2,0,4,3,8,1)], [0]),
    ][:count]
    with input_path.open("wb") as stream:
        stream.write(struct.pack("<Q7I", 0x3149534D44544545, 1, 32, 1, 1, 4, 4, count))
        stream.write(struct.pack("<4I", 4,4,4,4))
        for _demand, _net, source, sinks, rank, hops, _order in routes:
            stream.write(struct.pack("<7I", source,len(sinks),0,len(hops),0,max(h[0] for h in hops),rank))
            for sink in sinks:
                stream.write(struct.pack("<I",sink))
            for hop in hops:
                stream.write(struct.pack("<10I",*hop))
    run = subprocess.run([str(binary),"phase5","schedule",str(input_path),str(result_path)],capture_output=True,text=True)
    assert run.returncode == 0, run.stderr
    route_records, entry_records = [], []
    for demand,net,source,sinks,_rank,hops,order in sorted(routes,key=lambda item:item[0]):
        route_records.append({"id":demand,"net":net,"source":fpga[source],"sinks":[fpga[s] for s in sinks],
            "tree_edges":[{"link":links[hops[i][2]],"from":fpga[hops[i][3]],"to":fpga[hops[i][4]]} for i in order]})
    if routes:
        for sequence, route_index, hop_index, slot, lane, ready in [(0,0,0,1,0,1),(1,0,1,1,0,1),(3,1,0,1,3,1),(2,0,2,3,0,3)]:
            demand,net,_source,_sinks,_rank,hops,_order = routes[route_index]
            depth,capacity,link,source,sink,_offset,_budget,assigned,ratio,_latency = hops[hop_index]
            entry = {"id":f"s{sequence:06d}","demand":demand,"net":net,"hop":depth,"capacity_key":capacities[capacity],
                     "link":links[link],"from":fpga[source],"to":fpga[sink],"slot":slot,"lane":lane,"ready_slot":ready,"arrival_slot":slot+1}
            if assigned != missing:
                entry["tdm_ratio"] = ratio
            entry_records.append(entry)
    completions = [] if empty else [
        {"demand":"d0","net":"n1","source_ready_slot":1,"completion_slot":2,"sink_arrival_slots":{"c":2}},
        {"demand":routes[0][0],"net":routes[0][1],"source_ready_slot":1,"completion_slot":4,"sink_arrival_slots":{"c":2,"d":4}},
    ]
    expected = {"schema":"emuflow.tdm-schedule/v1","provider":"deterministic-dependency-earliest-slot-v1",
        "qualification":"dependency-schedule-readiness-pass","design":"typed record unit fixture","platform":"unit",
        "routes":route_records,"entries":entry_records,"demand_completions":completions,
        "domain_schedules":[{"key":key,"count":0 if empty else 1,"utilization":0.0 if empty else 1/128,
                             "nested":{"optional":None,"enabled":True,"labels":["汉字","a\t\"\\"]}} for key in capacities]}
    from emuflow.system_route_storage import write_system_route_storage, system_route_store_path, _demand_from_route
    public_routes = [{**route,"width_bits":1} for route in route_records]
    route_selector = tmp_path/"routes.json"
    write_system_route_storage(route_selector, {"schema":"emuflow.system-routes/v1", "routes":public_routes,
        "demands":[_demand_from_route(route) for route in public_routes],"timing_paths":[]})
    route_store_path = system_route_store_path(route_selector)
    native = {**expected,"routes":NoTraversal(route_records),"entries":NoTraversal(entry_records),"demand_completions":NoTraversal(completions)}
    source = {"schema":"emuflow.native-exact-schedule-publication-source/v1",
              "command":[str(binary),"phase5","publish-schedule"],"input":input_path,"result":result_path,"route_store":route_store_path,"catalogs":(fpga,links,capacities),
              "generations":[native_file_generation(p) for p in (input_path,result_path,route_store_path)],
              "route_records":native["routes"],"completion_records":native["demand_completions"]}
    native["entries"].native_store_publication_source = source
    return native, expected, source


@pytest.mark.parametrize("empty",[False,True])
def test_native_store_matches_reference_without_python_record_traversal(tmp_path,empty):
    native,expected,_source = _native_source(tmp_path,empty)
    reference_path,native_path = tmp_path/"reference"/"schedule.json",tmp_path/"native"/"schedule.json"
    reference = write_tdm_schedule_storage(reference_path,expected)
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",side_effect=AssertionError("Python publisher used")):
        result = write_tdm_schedule_storage(native_path,native)
    for key in ("schema","logical_sha256","logical_pretty_sha256","sections","entry_relation","route_relation","completion_relation"):
        assert result[key] == reference[key],key
    assert read_tdm_schedule(native_path,materialize=True) == expected
    with sqlite3.connect(tdm_schedule_store_path(native_path)) as actual, sqlite3.connect(":memory:") as schema:
        schema.executescript(FULLY_TYPED_DDL+TYPED_INDEX_DDL)
        assert actual.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall() == schema.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


@pytest.mark.parametrize("corruption",["changed_generation","truncated","trailing","hop","completion","route_edge","route_orphan","route_schema","request_failure","replaced_owner"])
def test_native_store_rejects_corruption_and_preserves_commit(tmp_path,corruption):
    native,expected,source = _native_source(tmp_path)
    manifest_path = tmp_path/"published"/"schedule.json"
    write_tdm_schedule_storage(manifest_path,native)
    previous = manifest_path.read_bytes()
    if corruption == "replaced_owner":
        native["routes"] = expected["routes"]
    elif corruption == "route_edge":
        with sqlite3.connect(source["route_store"]) as connection:
            connection.execute("UPDATE route_edges SET link_id='unknown' WHERE route_seq=0 AND seq=0")
    elif corruption == "route_orphan":
        with sqlite3.connect(source["route_store"]) as connection:
            connection.execute("INSERT INTO route_edges VALUES(999,0,'ab','a','b')")
    elif corruption == "route_schema":
        with sqlite3.connect(source["route_store"]) as connection:
            connection.execute("UPDATE metadata SET value=json_set(value,'$.schema','unknown') WHERE key='header'")
    elif corruption == "request_failure":
        native["qualification"]="unqualified"
    else:
        data=bytearray(source["result"].read_bytes())
        if corruption == "truncated": data=data[:-1]
        elif corruption in {"trailing","changed_generation"}: data+=b"x"
        elif corruption == "hop": data[32+8:32+12]=struct.pack("<I",999)
        else: data[-12:-8]=struct.pack("<I",999)
        source["result"].write_bytes(data)
    # Exercise the parser as well as stale-reference rejection. Explicitly
    # forging an updated local binding cannot make malformed records valid.
    if corruption not in {"changed_generation","replaced_owner","request_failure"}:
        source["generations"]=[native_file_generation(source[key]) for key in ("input","result","route_store")]
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",side_effect=AssertionError("silent Python fallback")):
        with pytest.raises(ValidationError):
            write_tdm_schedule_storage(manifest_path,native)
    assert manifest_path.read_bytes()==previous
    assert read_tdm_schedule(manifest_path,materialize=True)==expected
    assert not list(manifest_path.parent.glob(".*.tmp*"))
    assert not list(manifest_path.parent.glob("*.publishing.*"))


def test_unified_typed_route_producer_selects_native_store(tmp_path,monkeypatch):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    from emuflow.system_route_storage import write_system_route_storage,read_system_routes
    from emuflow.tdm import build_tdm_schedule
    case=StaticExactCombinationalCutPartitionTest();case.setUp()
    _,assignment=case._exact_artifacts(dependent_return=True)
    routes=case._exact_routes(assignment)
    routes["semantic_contract"]={**routes["semantic_contract"],"indexed":True}
    reference=build_tdm_schedule(routes,case.platform)
    route_path=tmp_path/"routes.json";write_system_route_storage(route_path,routes)
    native_bin = tmp_path/"install"/"bin"
    native_bin.mkdir(parents=True)
    (native_bin/"emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(native_bin.parent))
    monkeypatch.setattr("emuflow.tdm.NATIVE_EXACT_SCHEDULE_MIN_ROUTES",0)
    schedule=build_tdm_schedule(read_system_routes(route_path),case.platform)
    assert schedule["entries"].native_store_publication_source is not None
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",side_effect=AssertionError("Python publisher used")), \
         patch.object(type(schedule["entries"]),"__iter__",side_effect=AssertionError("Python entries used")), \
         patch.object(type(schedule["routes"]),"__iter__",side_effect=AssertionError("Python routes used")), \
         patch.object(type(schedule["demand_completions"]),"__iter__",side_effect=AssertionError("Python completions used")):
        write_tdm_schedule_storage(tmp_path/"native"/"schedule.json",schedule)
    assert read_tdm_schedule(tmp_path/"native"/"schedule.json",materialize=True)==reference

    # Exercise the real report-last Phase 5 boundary and independent reader,
    # rather than qualifying only the standalone store operation.
    from emuflow.phase5 import run_phase5, validate_phase5
    from tests.test_combinational_cut import PLATFORM_PATH
    persisted = case._persisted_routes(case._exact_routes(assignment))
    write_system_route_storage(route_path,persisted)
    assignment_path=tmp_path/"assignment.json"
    assignment_path.write_text(json.dumps(assignment),encoding="utf-8")
    output=tmp_path/"phase5"
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",side_effect=AssertionError("Python publisher used")):
        report=run_phase5(route_path,PLATFORM_PATH,output,assignment_path,managed_storage=True)
    assert report["status"]=="pass"
    assert report["schedule_storage"]["implementation_id"]=="native-exact-schedule-store-v1"
    assert validate_phase5(route_path,PLATFORM_PATH,output/"schedule.json",assignment_path=assignment_path)["status"]=="pass"
