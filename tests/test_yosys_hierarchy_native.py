"""Full hierarchy producer differential checks, not design qualification."""
import io
import json
from pathlib import Path
import random
import sqlite3
import subprocess

import pytest

from emuflow.errors import ImportError
from emuflow.native_protocol import CheckedRequestWriter
from emuflow.yosys_hierarchy_native import stage_hierarchy
from tests.native_build import emuflow_native
from tests.yosys_hierarchy_reference import (
    _stream_yosys_hierarchy_to_staging, _expand_staged_yosys_hierarchy,
)

DDL = (Path(__file__).resolve().parents[1]/"src/emuflow/data/schema/yosys_hierarchy.sql").read_text()


@pytest.fixture(scope="module")
def native():
    return str(emuflow_native())


def invoke(native, root, text, top="top"):
    request, store, summary = root/"request.bin", root/"staging.sqlite", root/"summary.json"
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QI", 0x31485953464D5545, 1)
        writer.text(top)
        writer.finish()
    finally:
        writer.close()
    completed = subprocess.run([native, "phase1", "stage-yosys-hierarchy", str(request), str(store), str(summary)], input=text, text=True, capture_output=True)
    return completed, store, summary


def reference(root, text):
    db = sqlite3.connect(root/"reference.sqlite")
    db.row_factory = sqlite3.Row
    # Former producer creates the module tables itself.
    db.executescript(DDL[DDL.index("CREATE TABLE staging_instances"):])
    creator = _stream_yosys_hierarchy_to_staging(Path("<stream>"), "top", db, source_stream=io.StringIO(text))
    def cell(name, value):
        from emuflow.resources import classify_primitive_resources
        resource = classify_primitive_resources(value["type"])
        if value["type"] != "$scopeinfo":
            db.execute("INSERT INTO staging_instances VALUES (?,?,?,?,?)", (name,value["type"],resource.ff,resource.bram,json.dumps(value)))
    def net(name, value):
        bits = value.get("bits", [])
        if not isinstance(bits, list):
            return
        db.executemany("INSERT OR IGNORE INTO staging_names VALUES (?,?,?,?,?,?)", ((bit,int(bool(value.get("hide_name",0))),len(name),name,i,len(bits)) for i,bit in enumerate(bits) if isinstance(bit,int)))
    ports, report, aliases = _expand_staged_yosys_hierarchy(db,"top",stage_cell=cell,stage_netname=net)
    cells = []
    for row in db.execute("SELECT * FROM staging_instances ORDER BY id"):
        value = json.loads(row[4])
        value["connections"] = {p:[aliases.get(b,b) if isinstance(b,int) else b for b in bits] for p,bits in value["connections"].items()}
        cells.append((*tuple(row)[:4],value))
    names = sorted(set((aliases.get(row[0],row[0]),*tuple(row)[1:]) for row in db.execute("SELECT * FROM staging_names") if isinstance(aliases.get(row[0],row[0]),int)))
    db.close()
    return creator,ports,report,cells,names


def circuit(seed):
    rng = random.Random(seed)
    modules = {"box":{"attributes":{"blackbox":"1"},"ports":{},"cells":{}}}
    leaf_ports = {"i":{"direction":"input","bits":[2]},"o":{"direction":"output","bits":[3]},"alias":{"direction":"output","bits":[3]},"constant":{"direction":"output","bits":["1"]},"open":{"direction":"input","bits":[4]}}
    modules["leaf"] = {"attributes":{},"ports":leaf_ports,"netnames":{"隐名":{"hide_name":1,"bits":[2,3,4]},"shown":{"bits":[2,3]}},"cells":{"lut":{"type":"LUT2","parameters":{"INIT":"1000"},"attributes":{"note":"α\\β"},"port_directions":{"I0":"input","I1":"input","O":"output"},"connections":{"I0":[2],"I1":[4],"O":[3]}},"bb":{"type":"box","connections":{"I":[2],"O":[5]}}}}
    top_cells = {}
    for i in rng.sample(range(8),8):
        top_cells[f"u{i}"] = {"type":"leaf","connections":{"i":[2],"o":[10+3*i],"alias":[11+3*i],"constant":[12+3*i],"open":[]}}
        top_cells[f"consumer{i}"] = {"type":"FDRE","connections":{"D":[12+3*i],"Q":[100+i]},"port_directions":{"D":"input","Q":"output"}}
    modules["top"] = {"attributes":{"top":"1"},"ports":{"clk":{"direction":"input","bits":[2]},"out":{"direction":"output","bits":[10,11,12]}},"netnames":{"out":{"bits":[10,11,12]},"clαk":{"bits":[2]}},"cells":top_cells}
    return {"creator":"native differential","modules":modules}


@pytest.mark.parametrize("seed",range(8))
def test_complete_hierarchy_matches_independent_reference(native,tmp_path,seed):
    text = json.dumps(circuit(seed),ensure_ascii=seed%2==0)
    expected = reference(tmp_path,text)
    completed,store,summary = invoke(native,tmp_path,text)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(summary.read_text())
    with sqlite3.connect(store) as db:
        cells = [(*row[:4],json.loads(row[4])) for row in db.execute("SELECT * FROM staging_instances ORDER BY id")]
        names = list(db.execute("SELECT * FROM staging_names ORDER BY yosys_bit,hidden,name_length,name,bus_index,bus_width"))
        ports = {r[0]:json.loads(r[1]) for r in db.execute("SELECT * FROM hierarchy_ports")}
    assert (result["creator"],ports,result["hierarchical_interchange"],cells,names) == expected


@pytest.mark.parametrize("text,reason",[
    ('{"modules":{}}',"missing top"),
    ('{"modules":{"top":{"cells":{"x":{"type":"top","connections":{}}}}}}',"recursive"),
    ('{"modules":{"top":{"cells":{"x":{"type":"LUT1","connections":{"I":[true]}}}}}}',"bit must"),
    ('{"modules":{"top":{}}} garbage',"trailing"),
    ('{"modules":{"top":{}},"modules":{}}',"duplicate"),
    ('{"modules":{"top":{"cells":{"x":{"type":"A","type":"B"}}}}}',"duplicate"),
    ('{"modules":{"top":{"ports":{"q":{"direction":"output","bits":[2]}}}',"truncated"),
])
def test_rejections_do_not_publish(native,tmp_path,text,reason):
    completed,store,summary = invoke(native,tmp_path,text)
    assert completed.returncode != 0
    assert reason in completed.stderr
    assert not store.exists() and not summary.exists()


def test_host_only_pumps_bytes_and_imports_indexed_rows(native,tmp_path,monkeypatch):
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.require_native_schema",lambda _:None)
    store = tmp_path/"host.sqlite"
    class Source(io.StringIO):
        def read(self,size=-1):
            assert 0 < size <= 65536
            return super().read(size)
    creator,ports,report,_ = stage_hierarchy(Path("<stream>"),store,"top",source_stream=Source(json.dumps(circuit(2))))
    assert creator == "native differential" and ports["out"]["bits"][-1] == "1"
    assert report["expanded_module_instances"] == 9
    with sqlite3.connect(store) as db:
        assert db.execute("SELECT count(*) FROM staging_instances").fetchone()[0] == 24
    assert not list(tmp_path.glob(".yosys-hierarchy-*"))


def test_late_attributes_and_repeated_deep_modules(native,tmp_path):
    value = circuit(1)
    # Attributes may follow cells and ports in the producer's document. A leaf
    # module marked whitebox must remain an instance rather than be expanded.
    value["modules"]["leaf"]["cells"]["bb"]["type"] = "box"
    value["modules"]["box"] = {"cells":{"invalid":{"type":"box","connections":{}}},"attributes":{"whitebox":"true"}}
    value["modules"]["middle"] = {"ports":value["modules"]["leaf"]["ports"],"cells":{"nested":{"type":"leaf","connections":{p:r["bits"] for p,r in value["modules"]["leaf"]["ports"].items()}}}}
    for cell in value["modules"]["top"]["cells"].values():
        if cell["type"] == "leaf":
            cell["type"] = "middle"
    text = json.dumps(value)
    expected = reference(tmp_path,text)
    completed,store,summary = invoke(native,tmp_path,text)
    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(store) as db:
        cells = [(*r[:4],json.loads(r[4])) for r in db.execute("SELECT * FROM staging_instances ORDER BY id")]
        names = list(db.execute("SELECT * FROM staging_names ORDER BY yosys_bit,hidden,name_length,name,bus_index,bus_width"))
        ports = {r[0]:json.loads(r[1]) for r in db.execute("SELECT * FROM hierarchy_ports")}
    result = json.loads(summary.read_text())
    assert (result["creator"],ports,result["hierarchical_interchange"],cells,names) == expected
    assert result["hierarchical_interchange"]["maximum_hierarchy_depth"] == 2


@pytest.mark.parametrize("raw",['{"ignored":1bad,"modules":{"top":{}}}', '{"ignored":"\\q","modules":{"top":{}}}'])
def test_ignored_subtrees_still_require_valid_json(native,tmp_path,raw):
    completed,store,summary = invoke(native,tmp_path,raw)
    assert completed.returncode != 0 and "invalid JSON" in completed.stderr
    assert not store.exists() and not summary.exists()


def test_stage_rejects_reused_or_aliased_destinations(native,tmp_path):
    output = tmp_path/"staging.sqlite"
    output.write_bytes(b"previous owner")
    completed,_,summary = invoke(native,tmp_path,json.dumps(circuit(0)))
    assert completed.returncode != 0 and "private and absent" in completed.stderr
    assert output.read_bytes() == b"previous owner" and not summary.exists()


def test_large_host_has_bounded_python_population(native,tmp_path,monkeypatch):
    import tracemalloc
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.require_native_schema",lambda _:None)
    source = tmp_path/"source.json"
    with source.open("w") as out:
        out.write('{"modules":{"top":{"cells":{')
        for i in range(20000):
            if i:
                out.write(',')
            out.write(json.dumps(f"cell{i:06d}")+':'+json.dumps({"type":"LUT1","connections":{"I0":[2],"O":[i+3]},"port_directions":{"I0":"input","O":"output"}}))
        out.write('}}}}')
    tracemalloc.start()
    try:
        _,_,report,_ = stage_hierarchy(source,tmp_path/"large.sqlite","top")
        _,peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4*1024*1024
    assert report["allocated_signal_bits"] == 20001
    with sqlite3.connect(tmp_path/"large.sqlite") as db:
        assert db.execute("SELECT count(*) FROM staging_instances").fetchone()[0] == 20000
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'staging_module_%'").fetchone()[0] == 0


def test_failed_producer_cancels_consumer_and_retires_private_writes(native,tmp_path,monkeypatch):
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.require_native_schema",lambda _:None)
    class Source:
        calls = 0
        def read(self,size):
            self.calls += 1
            if self.calls == 1:
                return '{"modules":{"top":{"cells":{'
            raise OSError("producer failed")
    with pytest.raises(OSError,match="producer failed"):
        stage_hierarchy(Path("<stream>"),tmp_path/"staging.sqlite","top",source_stream=Source())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value,reason",[
    ({"modules":{"top":{"cells":{"u":{"type":"leaf","connections":{"i":[2,3]}}}},"leaf":{"ports":{"i":{"direction":"input","bits":[2]}},"cells":{}}}},"width disagrees"),
    ({"modules":{"top":{"cells":{"a":{"type":"zero","connections":{"o":[2]}},"b":{"type":"one","connections":{"o":[2]}}}},"zero":{"ports":{"o":{"direction":"output","bits":["0"]}}},"one":{"ports":{"o":{"direction":"output","bits":["1"]}}}}},"incompatible constant"),
])
def test_electrical_hierarchy_conflicts_reject(native,tmp_path,value,reason):
    completed,store,summary = invoke(native,tmp_path,json.dumps(value))
    assert completed.returncode != 0 and reason in completed.stderr
    assert not store.exists() and not summary.exists()


def test_native_staging_reader_is_closed_before_adoption(native,tmp_path,monkeypatch):
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.require_native_schema",lambda _:None)
    import os
    connect,replace = sqlite3.connect,os.replace
    readers = []
    def open_reader(*args,**kwargs):
        reader = connect(*args,**kwargs)
        readers.append(reader)
        return reader
    def adopt(source,destination):
        assert readers
        for reader in readers:
            with pytest.raises(sqlite3.ProgrammingError,match="closed"):
                reader.execute("SELECT 1")
        return replace(source,destination)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.sqlite3.connect",open_reader)
    monkeypatch.setattr("emuflow.yosys_hierarchy_native.os.replace",adopt)
    stage_hierarchy(Path("<stream>"),tmp_path/"staging.sqlite","top",source_stream=io.StringIO(json.dumps(circuit(0))))
