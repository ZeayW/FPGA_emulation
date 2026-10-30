"""Full primitive normalization parity; not real-design qualification."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.storage.store_publication import bind_store_source
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from tests.xilinx_normalization_reference import normalize_xilinx_mapped_json, normalize_xilinx_indexed_staging
from tests.yosys_reference import import_yosys_json_indexed
from emuflow.frontend.yosys_hierarchy_native import stage_hierarchy
from emuflow.ir.ir import EmuIR
from tests.native_build import emuflow_native


@pytest.fixture(scope="module")
def native():
    return str(emuflow_native())


def cell(kind,connections,directions=None,**extra):
    return {"type":kind,"parameters":{},"attributes":{"src":"native-normalizer:α"},"connections":connections,"port_directions":directions or {p:"output" if p in {"O","CO","Q"} else "input" for p in connections},**extra}


def source(mode):
    first=cell("CARRY4",{"CI":[2],"CYINIT":[3 if mode%3==0 else "0"],"DI":[4,5,6,7],"S":[8,9,10,11],"CO":[12,13,14,15],"O":[16,17,18,19]},hide_name=0)
    second=cell("CARRY4",{"CI":[15],"CYINIT":["0"],"DI":[20,21,22,23],"S":[24,25,26,27],"CO":[28,29,30,31],"O":[32,33,34,35]})
    cells={"a_carry":first,"b_carry":second,"invert":cell("INV",{"I":[2],"O":[36]}),"mux":cell("MUXF7",{"I0":["1" if mode%2 else "0"],"I1":["0"],"S":[2],"O":[37]})}
    if mode%4==1:
        del cells["b_carry"]
    if mode%4==2:
        del first["connections"]["O"]
        del first["port_directions"]["O"]
    if mode%4==3:
        del first["connections"]["CO"]
        del first["port_directions"]["CO"]
    for i,kind in enumerate(["FDCE_1","FDPE_1","FDRE_1","FDSE_1"]):
        cells[f"ff{i}"] = cell(kind,{"D":[12+i],"C":[2],"Q":[40+i]},parameters={"IS_C_INVERTED":"1 "} if mode%2 else {})
    ports={"input":{"direction":"input","bits":[2,3,4,5,6,7,8,9,10,11]},"observe":{"direction":"output","bits":[12,13,14,15,16,17,18,19,28,29,30,31,32,33,34,35]}}
    if mode%5==4:
        ports.pop("observe")
    return {"creator":"normalization differential","modules":{"top":{"ports":ports,"cells":cells,"netnames":{"wide":{"bits":list(range(2,44))}}}}}


def invoke(native,root,value,library=DEFAULT_XILINX_PRIMITIVE_LIBRARY,mutate=None):
    request,store,summary=root/"request.bin",root/"staging.sqlite",root/"summary.json"
    ref=bind_store_source(library,"xilinx-primitive-library-v1")
    writer=CheckedRequestWriter(request)
    try:
        writer.put("QI",0x31485953464D5545,2)
        writer.text("top")
        writer.put("3Q4q",*ref.generation)
        writer.finish()
    finally:
        writer.close()
    if mutate:
        mutate()
    completed=subprocess.run([native,"phase1","stage-yosys-hierarchy",str(request),str(store),str(summary),str(library)],input=json.dumps(value),text=True,capture_output=True)
    return completed,store,summary


def rows(path):
    with sqlite3.connect(path) as db:
        cells={row[0]:json.loads(row[1]) for row in db.execute("SELECT id,record_json FROM staging_instances ORDER BY id")}
        ports={row[0]:json.loads(row[1]) for row in db.execute("SELECT name,record_json FROM hierarchy_ports ORDER BY name")}
    return cells,ports


@pytest.mark.parametrize("mode",range(12))
def test_complete_normalized_records_and_audit_match_independent_reference(native,tmp_path,monkeypatch,mode):
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.require_native_schema",lambda _:None)
    value=source(mode)
    _,_,_,normalization=stage_hierarchy(Path("<stream>"),tmp_path/"raw.sqlite","top",source_stream=io.StringIO(json.dumps(value)))
    assert normalization is None
    cells,ports=rows(tmp_path/"raw.sqlite")
    expected={"modules":{"top":{"cells":copy.deepcopy(cells),"ports":ports}}}
    report=normalize_xilinx_mapped_json(Path("<stream>"),None,top="top",_source_value=expected)
    completed,store,summary=invoke(native,tmp_path,value)
    assert completed.returncode==0,completed.stderr
    actual=json.loads(summary.read_text())
    actual_cells,actual_ports=rows(store)
    assert actual_cells==expected["modules"]["top"]["cells"]
    assert actual_ports==ports
    assert actual["normalization"]==report
    with sqlite3.connect(store) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'norm_%' OR name LIKE 'staging_module_%'").fetchone()[0]==0


def test_production_hierarchical_import_bypasses_python_normalizer(native,tmp_path,monkeypatch):
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.require_native_schema",lambda _:None)
    monkeypatch.setattr("tests.xilinx_normalization_reference.normalize_xilinx_mapped_json",lambda *a,**k:pytest.fail("production rebuilt a Python primitive population"))
    report=import_yosys_json_indexed(Path("<stream>"),tmp_path/"design.emuir.sqlite",top="top",_source_stream=io.StringIO(json.dumps(source(0))),_expand_hierarchy=True,staging_transform=normalize_xilinx_indexed_staging)
    assert report["carry8_paired_cells"]==1
    assert report["negedge_ff_normalized_cells"]==4
    ir=EmuIR.load(tmp_path/"design.emuir.sqlite")
    assert all(i["type"] not in {"CARRY4","INV","FDCE_1","FDPE_1","FDRE_1","FDSE_1"} for i in ir.value["instances"])


@pytest.mark.parametrize("change,reason",[
    (lambda v:v["modules"]["top"]["cells"].update(unknown=cell("NOT_A_PRIMITIVE",{})),"unsupported pre-normalization"),
    (lambda v:v["modules"]["top"]["cells"]["a_carry"]["connections"].update(DI=[2]),"width disagrees"),
    (lambda v:v["modules"]["top"]["cells"]["ff0"]["parameters"].update(IS_C_INVERTED="0"),"conflicting inverted-clock"),
    (lambda v:v["modules"]["top"]["cells"].update({"a_carry$carry_lut6_2_0":cell("LUT1",{})}),"helper identity collides"),
    (lambda v:v["modules"]["top"]["cells"]["ff0"].update(port_directions={"D":"not-a-direction"}),"directions are invalid"),
])
def test_invalid_sources_do_not_publish(native,tmp_path,change,reason):
    value=source(0);change(value)
    completed,store,summary=invoke(native,tmp_path,value)
    assert completed.returncode!=0 and reason in completed.stderr
    assert not store.exists() and not summary.exists()


def test_changed_library_generation_rejects(native,tmp_path):
    library=tmp_path/"library.json";library.write_bytes(DEFAULT_XILINX_PRIMITIVE_LIBRARY.read_bytes())
    completed,store,summary=invoke(native,tmp_path,source(0),library,lambda:library.write_bytes(library.read_bytes()+b" "))
    assert completed.returncode!=0 and "generation changed" in completed.stderr
    assert not store.exists() and not summary.exists()


def test_library_resource_contract_is_independently_checked(native,tmp_path):
    value=json.loads(DEFAULT_XILINX_PRIMITIVE_LIBRARY.read_text());value["cells"]["LUT1"]["resources"]["lut"]=2
    library=tmp_path/"library.json";library.write_text(json.dumps(value))
    completed,store,summary=invoke(native,tmp_path,source(0),library)
    assert completed.returncode!=0 and "resource contract disagrees" in completed.stderr
    assert not store.exists() and not summary.exists()


def test_large_negedge_population_uses_bounded_python_host(native,tmp_path,monkeypatch):
    import tracemalloc
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.resolve_native_executable",lambda _:native)
    monkeypatch.setattr("emuflow.frontend.yosys_hierarchy_native.require_native_schema",lambda _:None)
    input_path=tmp_path/"large.json"
    with input_path.open("w") as out:
        out.write('{"modules":{"top":{"ports":{"clk":{"direction":"input","bits":[2]}},"cells":{')
        for i in range(20000):
            if i:
                out.write(',')
            out.write(json.dumps(f"ff{i:06d}")+':'+json.dumps(cell("FDRE_1",{"C":[2],"D":[2],"Q":[i+3]},parameters=None,attributes="discarded non-object")))
        out.write('}}}}')
    tracemalloc.start()
    try:
        _,_,_,report=stage_hierarchy(input_path,tmp_path/"normalized.sqlite","top",normalization_library=DEFAULT_XILINX_PRIMITIVE_LIBRARY)
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak<4*1024*1024
    assert report["negedge_ff_normalized_cells"]==20000
    assert report["primitive_audit"]["resource_totals"]=={"ff":20000}
    assert report["helper_lut_cells"]==0
    with sqlite3.connect(tmp_path/"normalized.sqlite") as db:
        assert db.execute("SELECT count(*) FROM staging_instances WHERE type='FDRE' AND ff=1 AND json_extract(record_json,'$.parameters.IS_C_INVERTED')='1' AND json_extract(record_json,'$.attributes.emuflow_normalized')='yosys-negedge-ff-alias-v1'").fetchone()[0]==20000
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'norm_%'").fetchone()[0]==0
