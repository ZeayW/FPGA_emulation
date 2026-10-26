"""Full source -> compact owner differential qualification, not NVDLA acceptance."""
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.ir import EmuIR
from emuflow.yosys import import_yosys_json_indexed
from emuflow.xilinx_primitives import normalize_xilinx_mapped_json, normalize_xilinx_indexed_staging
from emuflow import yosys_hierarchy_native as host
from tests.native_build import emuflow_native
from tests.yosys_hierarchy_reference import _stream_yosys_hierarchy_to_staging,_expand_staged_yosys_hierarchy
from tests.test_xilinx_normalization_native import source as xilinx_source


@pytest.fixture(scope="module")
def native():
    return str(emuflow_native())


def old_source_to_emuir(value,output,clocks=(),normalize=False):
    with sqlite3.connect(":memory:") as db:
        db.row_factory=sqlite3.Row
        cells,names={},{}
        creator=_stream_yosys_hierarchy_to_staging(Path("<source>"),"top",db,source_stream=io.StringIO(json.dumps(value)))
        ports,hierarchy,aliases=_expand_staged_yosys_hierarchy(db,"top",stage_cell=lambda n,v:cells.update({n:v}),stage_netname=lambda n,v:names.update({n:v}))
    for cell in cells.values():
        cell["connections"]={p:[aliases.get(b,b) if isinstance(b,int) else b for b in bits] for p,bits in cell["connections"].items()}
    for net in names.values():
        net["bits"]=[aliases.get(b,b) if isinstance(b,int) else b for b in net["bits"]]
    flat={"creator":creator,"modules":{"top":{"ports":ports,"cells":cells,"netnames":names}}}
    if normalize:
        normalize_xilinx_mapped_json(Path("<source>"),None,top="top",_source_value=flat)
    import_yosys_json_indexed(Path("<source>"),output,top="top",clocks=clocks,_source_value=flat)
    return hierarchy


def payloads(path):
    with sqlite3.connect(path) as db:
        return list(db.execute("SELECT c.id,r.value,p.value,a.value,k.value FROM cell_records c JOIN payloads r ON r.seq=c.resources_seq JOIN payloads p ON p.seq=c.parameters_seq JOIN payloads a ON a.seq=c.attributes_seq JOIN payloads k ON k.seq=c.constants_seq ORDER BY c.seq"))


@pytest.mark.parametrize("mode",range(12))
def test_complete_hierarchical_xilinx_owner_matches_original(native,tmp_path,monkeypatch,mode):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    value=xilinx_source(mode)
    expected,actual=tmp_path/"expected.emuir.sqlite",tmp_path/"actual.emuir.sqlite"
    old_source_to_emuir(value,expected,normalize=True)
    with patch("emuflow.yosys.write_emuir_sqlite",side_effect=AssertionError("Python rebuilt final circuit rows")),patch("emuflow.yosys._indexed_endpoint_groups",side_effect=AssertionError("Python grouped endpoint population")):
        report=import_yosys_json_indexed(Path("<source>"),actual,top="top",_source_stream=io.StringIO(json.dumps(value)),_expand_hierarchy=True,staging_transform=normalize_xilinx_indexed_staging)
    assert EmuIR.load(actual).to_dict()==EmuIR.load(expected).to_dict()
    assert payloads(actual)==payloads(expected)
    assert report["native_emuir_import"]["validation"]["status"]=="pass"
    with sqlite3.connect(actual) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0]==0x454D5549
        assert db.execute("PRAGMA user_version").fetchone()[0]==2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'staging_%' OR name LIKE 'import_%' OR name LIKE 'norm_%' OR name LIKE 'hierarchy_%'").fetchall()


def generic_source():
    return {"creator":"test","modules":{"top":{"ports":{"clk":{"direction":"input","bits":[2]},"other_clock":{"direction":"input","bits":[3]},"reset":{"direction":"input","bits":[4]},"bidirectional":{"direction":"inout","bits":[5]},"output":{"direction":"output","bits":[6,"0","X"]}},"netnames":{"clk":{"bits":[2]},"reset":{"bits":[4]},"a":{"bits":[6]},"a[0]":{"bits":[7]},"a_alias":{"hide_name":1,"bits":[6,7]}},"cells":{"ff":{"type":"FDRE","parameters":{"wide":2**120+3,"float":0.12345678901234567,"negative_zero":-0.0,"mixed":[None,True,{"big":2**150,"str":"α\u0000β"}]},"connections":{"C":[2],"R":[4],"D":[6],"Q":[7]},"port_directions":{"C":"input","R":"input","D":"input","Q":"output"}},"combinational":{"type":"LUT1","connections":{"I0":[7],"O":[6]},"port_directions":{"I0":"input","O":"output"}},"unknown'dir":{"type":"CUSTOM","connections":{"missing":[6]}},"driver":{"type":"LUT1","connections":{"O":[5,6]},"port_directions":{"O":"output"}}}}}}


@pytest.mark.parametrize("clocks",[(),("clk",),("other_clock","clk","clk")])
def test_generic_types_clocks_warnings_and_wide_payloads(native,tmp_path,monkeypatch,clocks):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    value=generic_source();expected,actual=tmp_path/"expected.emuir.sqlite",tmp_path/"actual.emuir.sqlite"
    old_source_to_emuir(value,expected,clocks=clocks)
    import_yosys_json_indexed(Path("<source>"),actual,top="top",clocks=clocks,_source_stream=io.StringIO(json.dumps(value)),_expand_hierarchy=True)
    assert EmuIR.load(actual).to_dict()==EmuIR.load(expected).to_dict()
    assert payloads(actual)==payloads(expected)


@pytest.mark.parametrize("failure",["finalizer","source-change","publisher"])
def test_previous_published_owner_survives_rejected_generation(native,tmp_path,monkeypatch,failure):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    output=tmp_path/"result.emuir.sqlite";source=tmp_path/"source.json"
    source.write_text(json.dumps(generic_source()))
    import_yosys_json_indexed(source,output,top="top",_expand_hierarchy=True)
    previous=output.read_bytes()
    def finalize(connection):
        assert output.read_bytes()==previous
        if failure=="source-change":
            source.chmod(source.stat().st_mode ^ 0o100)
        if failure=="finalizer":
            raise RuntimeError("finalizer rejected")
    if failure=="publisher":
        monkeypatch.setattr(host,"publish_sqlite_store",lambda *a,**k:(_ for _ in ()).throw(RuntimeError("publisher rejected")))
    with pytest.raises(Exception,match="rejected|generation changed"):
        import_yosys_json_indexed(source,output,top="top",_expand_hierarchy=True,_finalize_emuir=finalize)
    assert output.read_bytes()==previous
    assert not list(tmp_path.glob(".yosys-emuir-*"))


def test_net_identity_collision_policy_and_unicode_clock_inference(native,tmp_path,monkeypatch):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    value={"modules":{"top":{"ports":{"clK":{"direction":"input","bits":[8]},"reſet":{"direction":"input","bits":[9]}},"netnames":{"a":{"bits":[2,3]},"a[0]":{"bits":[4]},"clK":{"bits":[8]},"reſet":{"bits":[9]}},"cells":{f"d{i}":{"type":"LUT1","connections":{"I0":["0"],"O":[i]},"port_directions":{"I0":"input","O":"output"}} for i in (2,3,4)}}}}
    expected,actual=tmp_path/"old.emuir.sqlite",tmp_path/"new.emuir.sqlite"
    old_source_to_emuir(value,expected)
    import_yosys_json_indexed(Path("<source>"),actual,top="top",_source_stream=io.StringIO(json.dumps(value)),_expand_hierarchy=True)
    assert EmuIR.load(actual).to_dict()==EmuIR.load(expected).to_dict()
    assert any('#' in n["id"] for n in EmuIR.load(actual).value["nets"])


@pytest.mark.parametrize("artifact",["store","summary"])
def test_original_producer_generation_is_required(native,tmp_path,monkeypatch,artifact):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    output=tmp_path/"result.emuir.sqlite"
    import_yosys_json_indexed(Path("<source>"),output,top="top",_source_stream=io.StringIO(json.dumps(generic_source())),_expand_hierarchy=True)
    previous=output.read_bytes()
    finish=host.NativeToolProcess.finish
    def mutate(process):
        result=finish(process)
        if "import-yosys" in process.command:
            index=process.command.index("import-yosys")
            target=Path(process.command[index+(2 if artifact=="store" else 3)])
            if artifact=="store":
                with sqlite3.connect(target) as db:
                    db.execute("UPDATE metadata SET value='[\"altered\"]' WHERE key='warnings_json'")
            else:
                target.write_text(target.read_text()+" \n")
        return result
    monkeypatch.setattr(host.NativeToolProcess,"finish",mutate)
    with pytest.raises(Exception,match="generation changed"):
        import_yosys_json_indexed(Path("<source>"),output,top="top",_source_stream=io.StringIO(json.dumps(generic_source())),_expand_hierarchy=True)
    assert output.read_bytes()==previous
    assert not list(tmp_path.glob(".yosys-emuir-*"))


def test_large_fanout_has_no_python_endpoint_or_net_population(native,tmp_path,monkeypatch):
    import tracemalloc
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    source,output=tmp_path/"large.json",tmp_path/"large.emuir.sqlite"
    with source.open("w") as out:
        out.write('{"modules":{"top":{"ports":{"clk":{"direction":"input","bits":[2]}},"netnames":{"clk":{"bits":[2]}},"cells":{')
        for i in range(20000):
            if i:
                out.write(',')
            out.write(json.dumps(f"ff{i:06d}")+':'+json.dumps({"type":"FDRE","parameters":{},"attributes":{},"connections":{"C":[2],"D":[2],"Q":[i+3]},"port_directions":{"C":"input","D":"input","Q":"output"}}))
        out.write('}}}}')
    tracemalloc.start()
    try:
        with patch("emuflow.yosys.write_emuir_sqlite",side_effect=AssertionError("Python writer called")),patch("emuflow.yosys._indexed_endpoint_groups",side_effect=AssertionError("Python endpoints grouped")):
            result=import_yosys_json_indexed(source,output,top="top",_expand_hierarchy=True)
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak<4*1024*1024
    assert result["native_emuir_import"]["records"]=={"cell_records":20000,"net_records":20001,"endpoint_records":60001}
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT fanout FROM net_records WHERE cut_class='clock'").fetchone()[0]==40000
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'staging_%' OR name LIKE 'import_%' OR name LIKE 'hierarchy_%'").fetchall()
        assert db.execute("PRAGMA freelist_count").fetchone()[0]*db.execute("PRAGMA page_size").fetchone()[0]<output.stat().st_size//10


def test_normalized_negedge_preserves_arbitrary_payload_numbers(native,tmp_path,monkeypatch):
    monkeypatch.setattr(host,"resolve_native_executable",lambda _:native)
    monkeypatch.setattr(host,"require_native_schema",lambda _:None)
    value=xilinx_source(0)
    ff=value["modules"]["top"]["cells"]["ff0"]
    ff["parameters"]["wide"]=2**192+17
    ff["parameters"]["precision"]=0.12345678901234567
    ff["attributes"]["nested"]={"wide":2**180+19,"float":1.2345678901234567e-15,"array":[1e-200,-0.0]}
    expected,actual=tmp_path/"old.emuir.sqlite",tmp_path/"new.emuir.sqlite"
    old_source_to_emuir(value,expected,normalize=True)
    import_yosys_json_indexed(Path("<source>"),actual,top="top",_source_stream=io.StringIO(json.dumps(value)),_expand_hierarchy=True,staging_transform=normalize_xilinx_indexed_staging)
    assert payloads(actual)==payloads(expected)


def test_hierarchy_has_no_custom_python_transform_fallback(tmp_path):
    calls=[]
    with pytest.raises(Exception,match="current native Xilinx normalization"):
        import_yosys_json_indexed(Path("<source>"),tmp_path/"result.emuir.sqlite",top="top",_source_stream=io.StringIO(json.dumps(generic_source())),_expand_hierarchy=True,staging_transform=lambda *a:calls.append(a))
    assert not calls
    assert not list(tmp_path.iterdir())
