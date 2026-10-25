import csv
import json
from pathlib import Path
import sqlite3

import pytest

from emuflow.errors import ValidationError
from emuflow.vivado_lineage import INVENTORY_HEADER, import_cell_lineage, resolve_timing_query
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.yosys import import_yosys_json


def fixture(root, *, change=None, retained_source=False, transformations="RETARGET EQU_REWIRE_OPT", noise=0):
    def ff(reset, q, *, clock=2, ce=3, data=4):
        return dict(type="FDCE", parameters={"INIT":"x"}, attributes={},
                    port_directions={"C":"input","CE":"input","D":"input","CLR":"input","Q":"output"},
                    connections={"C":[clock],"CE":[ce],"D":[data],"CLR":[reset],"Q":[q]})
    def inv(output):
        return dict(type="LUT1",parameters={"INIT":"01"},attributes={},
                    port_directions={"I0":"input","O":"output"},connections={"I0":[7],"O":[output]})
    cells={"source":ff(5,6),"kept":ff(8,9),"reset_a":inv(5),"reset_b":inv(8)}
    if noise:cells["unrelated"]=ff(8,11,data=10)
    if change == "reset-input":cells["reset_b"]["connections"]["I0"]=[10]
    if change == "reset-function":cells["reset_b"]["parameters"]["INIT"]="10"
    if change == "unknown-reset":
        cells["reset_a"]["parameters"]["INIT"]="xx"
        cells["reset_b"]["parameters"]["INIT"]="xx"
    if change == "clock":cells["kept"]["connections"]["C"]=[10]
    if change == "data":cells["kept"]["connections"]["D"]=[10]
    if change == "enable":cells["kept"]["connections"]["CE"]=[10]
    if change == "init":cells["kept"]["parameters"]["INIT"]="1"
    ports={name:{"direction":"input","bits":[bit]} for name,bit in (("clk",2),("ce",3),("d",4),("reset",7),("other",10))}
    ports["q"]={"direction":"output","bits":[9]}
    source=root/"input.json";source.write_text(json.dumps({"modules":{"dut":{"attributes":{"top":"1"},"ports":ports,"cells":cells,"netnames":{}}}}))
    ir=root/"ir.emuir.sqlite";import_yosys_json(source).save(ir)
    report=emit_vivado_mapped_verilog(ir,root/"dut.v")
    mapped,functions=Path(report["mapped_index"]["path"]),Path(report["source_functions"]["path"])
    with sqlite3.connect(mapped) as db:origins=dict(db.execute("SELECT id,seq FROM cells"))
    inventory=root/"inventory.tsv"
    with inventory.open("w") as stream:
        writer=csv.writer(stream,delimiter="\t",lineterminator="\n")
        writer.writerow(INVENTORY_HEADER)
        writer.writerow(["p/kept","FDCE",f"s{origins['kept']}",transformations.encode().hex(),"SLICE_X0Y0","AFF","0","0","LEAF"])
        if retained_source:
            writer.writerow(["p/source","FDCE",f"s{origins['source']}","","SLICE_X1Y0","AFF","1","0","LEAF"])
        for index in range(noise):
            writer.writerow([f"p/unrelated_{index}","FDCE",f"s{origins['unrelated']}","",
                             f"SLICE_X{index+2}Y0","AFF",str(index+2),"0","LEAF"])
    lineage=root/"lineage.sqlite"
    import_cell_lineage(mapped,functions,{"routed":inventory},lineage,mapped_sha256=report["mapped_index"]["sha256"])
    query=root/"query.tsv"
    query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"source".encode().hex()+"\n")
    return mapped,functions,lineage,query


def test_identical_reset_inverters_prove_ff_association_without_preserving_cells(tmp_path):
    result=resolve_timing_query(*fixture(tmp_path),tmp_path/"resolved.tsv",boundary=True)
    assert result["queries"] == 1
    with (tmp_path/"resolved.tsv").open() as stream:row=next(csv.DictReader(stream,delimiter="\t"))
    assert bytes.fromhex(row["resolved_boundary_cells_hex"]).decode() == "p/kept"


@pytest.mark.parametrize("change", ["reset-input","reset-function","unknown-reset","clock","data","enable","init"])
def test_a_different_clock_data_control_or_initialization_does_not_alias(tmp_path,change):
    with pytest.raises(ValidationError,match="no proven endpoint association"):
        resolve_timing_query(*fixture(tmp_path,change=change),tmp_path/"resolved.tsv",boundary=True)


def test_a_retained_source_is_not_broadened_by_control_equivalence(tmp_path):
    resolve_timing_query(*fixture(tmp_path,retained_source=True),tmp_path/"resolved.tsv",boundary=True)
    with (tmp_path/"resolved.tsv").open() as stream:row=next(csv.DictReader(stream,delimiter="\t"))
    assert bytes.fromhex(row["resolved_boundary_cells_hex"]).decode() == "p/source"


def test_structural_aliasing_does_not_admit_a_sequential_transformation(tmp_path):
    with pytest.raises(ValidationError,match="unsupported sequential transformation"):
        resolve_timing_query(*fixture(tmp_path,transformations="REGISTER_OPT"),tmp_path/"resolved.tsv",boundary=True)


def test_control_alias_lookup_does_not_scan_unrelated_stage_objects(tmp_path,monkeypatch):
    inputs=fixture(tmp_path,noise=25000)
    original=sqlite3.connect
    ticks=[0]
    def bounded(*args,**kwargs):
        connection=original(*args,**kwargs)
        def progress():
            ticks[0]+=1
            return int(ticks[0]>10)
        connection.set_progress_handler(progress,1000)
        return connection
    monkeypatch.setattr("emuflow.vivado_lineage.sqlite3.connect",bounded)
    resolve_timing_query(*inputs,tmp_path/"resolved.tsv",boundary=True)
    with (tmp_path/"resolved.tsv").open() as stream:row=next(csv.DictReader(stream,delimiter="\t"))
    assert bytes.fromhex(row["resolved_boundary_cells_hex"]).decode() == "p/kept"
