from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.vivado_constant_frontier import require_equal_frontier_functions


def test_pin_permutation_is_checked_by_function_not_pin_spelling():
    # Source I0 is the proposed constant, while the two live inputs swap pins.
    # f = I1 & ~I2; native g = I0 & ~I1.
    source = {"type":"LUT3", "init":"00001100", "inputs":{"I0":0,"I1":"s4/Q","I2":"s8/Q"}}
    native = {"type":"LUT3", "init":"8'h22", "inputs":{"I0":"s4/Q","I1":"s8/Q","I2":0}}
    assert require_equal_frontier_functions(source,native) == {"variables":2,"assignments":4}
    native["inputs"]["I0"],native["inputs"]["I1"] = native["inputs"]["I1"],native["inputs"]["I0"]
    with pytest.raises(ValidationError,match="does not confirm"):
        require_equal_frontier_functions(source,native)


def test_ground_pin_and_equal_init_alone_cannot_confirm_selection():
    source = {"type":"LUT2","init":"0110","inputs":{"I0":0,"I1":"s7/Q"}}
    native = {"type":"LUT2","init":"4'h6","inputs":{"I0":0,"I1":"unrelated-native-driver"}}
    with pytest.raises(ValidationError,match="does not confirm"):
        require_equal_frontier_functions(source,native)


def test_functionally_irrelevant_unassociated_input_is_not_a_false_mismatch():
    source = {"type":"LUT3","init":"10101100","inputs":{"I0":0,"I1":0,"I2":"s7/Q"}}
    native = {"type":"LUT1","init":"2'h0","inputs":{"I0":"unassociated-native-driver"}}
    assert require_equal_frontier_functions(source,native)["assignments"] == 4


@pytest.mark.parametrize("mutation", [
    {"type":"FDRE"}, {"init":"4'h10"}, {"init":"4'b0201"}, {"init":"4'hx"},
    {"init":"2'h0"}, {"inputs":{"I0":False,"I1":"s7/Q"}}, {"inputs":{"I0":0}},
])
def test_incomplete_or_unknown_primitive_facts_fail_closed(mutation):
    original = {"type":"LUT2","init":"0110","inputs":{"I0":0,"I1":"s7/Q"}}
    with pytest.raises(ValidationError):
        require_equal_frontier_functions(original,{**original,**mutation})


def test_selected_native_observations_use_indexed_source_driver_identity(tmp_path):
    import csv
    from tests.test_vivado_optimization import VivadoOptimizationTests
    from emuflow.vivado_optimization import build_optimization_proofs
    from emuflow.vivado_constant_frontier import compare_selected_constant_frontiers
    mapped,lineage,digest,origins = VivadoOptimizationTests().prepare(tmp_path)
    proofs = tmp_path/"proofs.sqlite"
    build_optimization_proofs(mapped,lineage,proofs,mapped_sha256=digest,fpga="fpga0")
    observed = tmp_path/"frontiers.tsv"
    headers = ["source_origin","native_cell_hex","ref","init","pin","driver_kind","driver_source","driver_ref","driver_pin","driver_name_hex","driver_transformations_hex","transformations_hex"]
    row = [f"s{origins['dead_feedback']}","partition/dead_feedback".encode().hex(),"LUT1","2'h2","I0","source",
           f"s{origins['dead']}","FDRE","Q","partition/dead/Q".encode().hex(),"",""]
    def write():
        with observed.open("w") as stream:
            writer = csv.writer(stream,delimiter="\t",lineterminator="\n");writer.writerow(headers);writer.writerow(row)
    write()
    expected = [{"source_origin":row[0],"name":"partition/dead_feedback"}]
    def compare():
        return compare_selected_constant_frontiers(mapped,mapped.with_name("dut.functions.sqlite"),proofs,observed,
                                                   mapped_sha256=digest,expected_objects=expected)
    assert compare()["frontiers"][0]["variables"] == 1
    row[6] = f"s{origins['kept']}";write()
    with pytest.raises(ValidationError,match="does not confirm"): compare()
    row[6] = f"s{origins['dead']}";row[10] = "RETIMED".encode().hex();write()
    with pytest.raises(ValidationError,match="unsupported transformation"): compare()
    row[10] = "";row[4] = "I1";write()
    with pytest.raises(ValidationError,match="input coverage"): compare()


def conditional_model(tmp_path, *, expose_state=False, duplicate_frontier=False, dependent=False, state_chain=None, dependency_output=False):
    import csv
    import json
    from emuflow.yosys import import_yosys_json
    from emuflow.vivado_netlist import emit_vivado_mapped_verilog
    from emuflow.vivado_lineage import import_cell_lineage, INVENTORY_HEADER
    from emuflow.vivado_optimization import build_optimization_proofs
    import sqlite3
    cells = {
        "kept":{"type":"FDRE","parameters":{"INIT":"0"},"attributes":{},
                "port_directions":{"C":"input","CE":"input","D":"input","R":"input","Q":"output"},
                "connections":{"C":[2],"CE":["1"],"D":[3],"R":["0"],"Q":[4]}},
        "conditional":{"type":"FDRE","parameters":{"INIT":"x"},"attributes":{},
                "port_directions":{"C":"input","CE":"input","D":"input","R":"input","Q":"output"},
                "connections":{"C":[2],"CE":[3],"D":["0"],"R":["0"],"Q":[5]}},
        "front":{"type":"LUT2","parameters":{"INIT":"0110"},"attributes":{},
                "port_directions":{"I0":"input","I1":"input","O":"output"},
                "connections":{"I0":[5],"I1":[4],"O":[6]}},
    }
    if dependent:
        cells["upstream"]={"type":"FDRE","parameters":{"INIT":"x"},"attributes":{},
            "port_directions":{"C":"input","CE":"input","D":"input","R":"input","Q":"output"},
            "connections":{"C":[2],"CE":["0"],"D":[3],"R":["0"],"Q":[7]}}
        cells["conditional"]["connections"]["D"]=[7]
    if state_chain:
        cells["relay"]={"type":"FDRE","parameters":{"INIT":"x"},"attributes":{},
            "port_directions":{"C":"input","CE":"input","D":"input","R":"input","Q":"output"},
            "connections":{"C":[2],"CE":["1"],"D":[5],"R":["0"],"Q":[8]}}
        cells["front"]["connections"]["I0"]=[8]
        if state_chain == "cycle":
            cells["conditional"]["connections"]["D"]=[8]
        if state_chain == "dynamic":
            cells["relay"]["connections"]["D"]=[3]
        if state_chain == "output":
            expose_state=True
    source=tmp_path/"source.json"
    source.write_text(json.dumps({"modules":{"dut":{"attributes":{"top":"1"},"cells":cells,
        "ports":{"clk":{"direction":"input","bits":[2]},"d":{"direction":"input","bits":[3]},
                 "q":{"direction":"output","bits":[6,7] if dependency_output else [6,5] if expose_state else [6]}},"netnames":{}}}}))
    ir=tmp_path/"ir.emuir.sqlite";import_yosys_json(source).save(ir)
    report=emit_vivado_mapped_verilog(ir,tmp_path/"dut.v")
    mapped=Path(report["mapped_index"]["path"]);functions=Path(report["source_functions"]["path"])
    with sqlite3.connect(mapped) as db:origins=dict(db.execute("SELECT id,seq FROM cells"))
    inventory=tmp_path/"inventory.tsv"
    with inventory.open("w") as stream:
        writer=csv.writer(stream,delimiter="\t",lineterminator="\n");writer.writerow(INVENTORY_HEADER)
        writer.writerow(["p/kept","FDRE",f"s{origins['kept']}",""])
        writer.writerow(["p/front","LUT2",f"s{origins['front']}",""])
        if duplicate_frontier:writer.writerow(["p/front_clone","LUT2",f"s{origins['front']}",""])
    lineage=tmp_path/"lineage.sqlite"
    digest=report["mapped_index"]["sha256"]
    import_cell_lineage(mapped,functions,{"synthesized":inventory},lineage,mapped_sha256=digest,retain_stage="synthesized")
    proof=tmp_path/"proof.sqlite";build_optimization_proofs(mapped,lineage,proof,mapped_sha256=digest,stage="synthesized",fpga="fpga0")
    observed=tmp_path/"native.tsv"
    with observed.open("w") as stream:
        writer=csv.writer(stream,delimiter="\t",lineterminator="\n")
        writer.writerow(["source_origin","native_cell_hex","ref","init","pin","driver_kind","driver_source","driver_ref","driver_pin","driver_name_hex","driver_transformations_hex","transformations_hex"])
        for pin,kind,origin,ref,dname in [("I0","zero","","GND","GND/G"),("I1","source",f"s{origins['kept']}","FDRE","p/kept/Q")]:
            writer.writerow([f"s{origins['front']}","p/front".encode().hex(),"LUT2","4'h6",pin,kind,origin,ref,"Q",dname.encode().hex(),"",""])
    return mapped,functions,proof,inventory,observed,digest,origins


def test_constant_confirmation_checks_complete_source_fanout_and_native_snapshot(tmp_path):
    from emuflow.vivado_constant_frontier import confirm_selected_constant_sources
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path)
    def confirm(selected=None):
        return confirm_selected_constant_sources(mapped,functions,proof,observed,inventory,mapped_sha256=digest,
            candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,fpga="fpga0",
            selected_sources=[f"s{origins['conditional']}"] if selected is None else selected)
    cert=confirm()
    assert cert["sources"] == [{"source":f"s{origins['conditional']}","constant_value":0,
                               "source_initialization":"vendor-dont-care-selection","frontier_sources":[f"s{origins['front']}"]}]
    assert cert["frontiers"][0]["assignments"] == 2
    for bad in ([{}], ["s00"], [f"s{origins['kept']}"]):
        with pytest.raises(ValidationError):confirm(bad)
    observed.write_text(observed.read_text().replace("4'h6","4'h9"))
    with pytest.raises(ValidationError,match="pin bindings"):confirm()


@pytest.mark.parametrize("variant,match", [("output","output port"),("replica","incomplete")])
def test_unobserved_output_or_native_clone_prevents_constant_confirmation(tmp_path,variant,match):
    from emuflow.vivado_constant_frontier import confirm_selected_constant_sources
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,expose_state=variant=="output",duplicate_frontier=variant=="replica")
    with pytest.raises(ValidationError,match=match):
        confirm_selected_constant_sources(mapped,functions,proof,observed,inventory,mapped_sha256=digest,
            candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])


@pytest.mark.parametrize("chain",[None,"pipeline","cycle"])
def test_only_checked_constant_sources_can_enter_a_bound_routed_proof(tmp_path,chain):
    import sqlite3
    import json
    import hashlib
    from emuflow.vivado_constant_frontier import check_constant_sources, CheckedConstantSources
    from emuflow.vivado_lineage import import_cell_lineage
    from emuflow.vivado_optimization import build_optimization_proofs, OptimizationProofView
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,state_chain=chain)
    args=(mapped,functions,proof,observed,inventory)
    kwargs=dict(mapped_sha256=digest,candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,
                fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])
    checked=check_constant_sources(*args,**kwargs)
    with pytest.raises(ValidationError,match="semantic checker"):
        CheckedConstantSources(checked.to_document())
    copy=checked.to_document();copy["sources"][0]["constant_value"]=1
    assert checked["sources"][0]["constant_value"] == 0
    routed_lineage=tmp_path/"routed-lineage.sqlite"
    import_cell_lineage(mapped,functions,{"synthesized":inventory,"routed":inventory},routed_lineage,mapped_sha256=digest)
    final=tmp_path/"routed-proof.sqlite"
    for supplied,checkpoint in [(checked.to_document(),"a"*64),(checked,"b"*64)]:
        with pytest.raises(ValidationError):
            build_optimization_proofs(mapped,routed_lineage,final,mapped_sha256=digest,fpga="fpga0",
                                      checked_constants=supplied,synthesized_checkpoint_sha256=checkpoint)
    report=build_optimization_proofs(mapped,routed_lineage,final,mapped_sha256=digest,fpga="fpga0",
                                     checked_constants=checked,synthesized_checkpoint_sha256="a"*64)
    assert report["vendor_confirmed_constant_state_cells"] == (1 if chain is None else 2)
    assert report["conditional_constant_state_candidates"] == 0
    with OptimizationProofView(final,expected_fpga="fpga0") as view:
        result=view.require_eliminated_source(f"s{origins['conditional']}")
        assert result["constant_value"] == 0
        assert result["initialization"] == "vendor-confirmed"
    # Even a refreshed document digest cannot make a different constant
    # agree with the proof rows sealed by the source-model owner.
    with sqlite3.connect(final) as db:
        document=json.loads(db.execute("SELECT document FROM vendor_confirmation_document").fetchone()[0])
        document["schema"]="emuflow.vivado-selected-constant-confirmations/v1"
        raw=json.dumps(document,sort_keys=True,separators=(",",":"))
        db.execute("UPDATE vendor_confirmation_document SET document=?",(raw,))
        db.execute("UPDATE metadata SET value=? WHERE key='vendor_confirmation_document_sha256'",
                   (json.dumps(hashlib.sha256(raw.encode()).hexdigest()),))
    with pytest.raises(ValidationError,match="source or initialization policy"):
        OptimizationProofView(final,expected_fpga="fpga0")
    with sqlite3.connect(final) as db:
        from emuflow.vivado_constant_frontier import CONSTANT_CONFIRMATION_SCHEMA
        document["schema"]=CONSTANT_CONFIRMATION_SCHEMA
        document["sources"][0]["constant_value"]=1
        raw=json.dumps(document,sort_keys=True,separators=(",",":"))
        db.execute("UPDATE vendor_confirmation_document SET document=?",(raw,))
        db.execute("UPDATE metadata SET value=? WHERE key='vendor_confirmation_document_sha256'",
                   (json.dumps(hashlib.sha256(raw.encode()).hexdigest()),))
    with pytest.raises(ValidationError,match="selection differs"):
        OptimizationProofView(final,expected_fpga="fpga0")


def test_synthesized_preflight_does_not_claim_routed_query_resolution(tmp_path):
    from emuflow.vivado_constant_frontier import check_constant_sources
    from emuflow.vivado_optimization import build_optimization_proofs, OptimizationProofView
    from emuflow.vivado_lineage import resolve_timing_query
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path)
    checked=check_constant_sources(mapped,functions,proof,observed,inventory,mapped_sha256=digest,
        candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])
    lineage=tmp_path/"lineage.sqlite";final=tmp_path/"confirmed-synth.sqlite"
    build_optimization_proofs(mapped,lineage,final,mapped_sha256=digest,stage="synthesized",fpga="fpga0",
                              checked_constants=checked,synthesized_checkpoint_sha256="a"*64)
    with pytest.raises(ValidationError,match="stage disagrees"):
        OptimizationProofView(final,expected_fpga="fpga0")
    query=tmp_path/"query.tsv"
    query.write_text("endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\n"+
                     "\t".join(["p".encode().hex(),"capture","pin","kept/Q".encode().hex(),"pin","conditional/D".encode().hex(),"",""])+"\n")
    result=resolve_timing_query(mapped,functions,lineage,query,tmp_path/"resolved.tsv",optimization_proofs=final,
                                allow_eliminated=True,stage="synthesized")
    assert result["stage"] == "synthesized"
    assert f"optimized-away:s{origins['conditional']}" in (tmp_path/"resolved.tsv").read_text()
    with pytest.raises(ValidationError,match="different source or stage"):
        resolve_timing_query(mapped,functions,lineage,query,tmp_path/"wrong-stage.tsv",optimization_proofs=final,allow_eliminated=True)


def test_constant_proof_does_not_borrow_unselected_initialization_hypotheses(tmp_path):
    from emuflow.vivado_constant_frontier import confirm_selected_constant_sources, compare_selected_constant_frontiers
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,dependent=True)
    # A hypothetical all-zero source model matches the native LUT, but that
    # alone does not prove the selected state without its upstream INIT choice.
    result=compare_selected_constant_frontiers(mapped,functions,proof,observed,mapped_sha256=digest,
        expected_objects=[{"source_origin":f"s{origins['front']}","name":"p/front"}])
    assert result["status"] == "pass"
    import sqlite3
    from emuflow.vivado_constant_frontier import _require_inductive_selected_states, _UnconfirmedStateDependencies
    with sqlite3.connect(mapped) as db:
        db.execute("ATTACH DATABASE ? AS proofs",(str(proof),))
        with pytest.raises(_UnconfirmedStateDependencies) as error:
            _require_inductive_selected_states(db,mapped,{origins['conditional']:0})
        assert error.value.dependencies == (origins['upstream'],)
    # Closure may refine both states, but the extra choice must be explicit
    # in the complete source record set and have all its fanouts checked.
    confirmed=confirm_selected_constant_sources(mapped,functions,proof,observed,inventory,mapped_sha256=digest,
        candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])
    assert {row['source'] for row in confirmed['sources']} == {f"s{origins['conditional']}",f"s{origins['upstream']}"}


def test_functional_equivalence_alone_is_not_a_synthesis_pin_projection(tmp_path):
    from emuflow.vivado_constant_frontier import compare_selected_constant_frontiers
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path)
    # XOR allows swapping pins without changing the function. A diagnostic
    # accepts that equality; a constant-selection proof requires the actual
    # source signal's pin projection before physical optimization.
    text=observed.read_text();text=text.replace("\tI0\t","\tTEMP\t").replace("\tI1\t","\tI0\t").replace("\tTEMP\t","\tI1\t")
    observed.write_text(text)
    options=dict(mapped_sha256=digest,expected_objects=[{"source_origin":f"s{origins['front']}","name":"p/front"}],
                 selected_constant_values={origins['conditional']:0})
    assert compare_selected_constant_frontiers(mapped,functions,proof,observed,**options)["status"] == "pass"
    with pytest.raises(ValidationError,match="pin bindings"):
        compare_selected_constant_frontiers(mapped,functions,proof,observed,require_input_identity=True,**options)


@pytest.mark.parametrize("chain",["pipeline","cycle"])
def test_closed_state_component_is_planned_before_native_observation(tmp_path,chain):
    from emuflow.vivado_constant_frontier import prepare_constant_frontier_query, confirm_selected_constant_sources
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,state_chain=chain)
    options=dict(mapped_sha256=digest,candidate_proofs_sha256=file_sha256(proof),
                 checkpoint_sha256="a"*64,fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])
    plan=prepare_constant_frontier_query(mapped,proof,inventory,**options)
    assert plan["status"] == "inspection-required"
    assert {r["source"] for r in plan["sources"]} == {f"s{origins['conditional']}",f"s{origins['relay']}"}
    assert plan["query_objects"] == [{"source_origin":f"s{origins['front']}","name":"p/front"}]
    for row in plan["sources"]:
        assert row["frontier_sources"] == [f"s{origins['front']}"]
    result=confirm_selected_constant_sources(mapped,functions,proof,observed,inventory,**options)
    assert result["sources"] == plan["sources"]
    # A real live input cannot be substituted just because both states were
    # selected. All native pins remain part of the same source projection.
    observed.write_text(observed.read_text().replace("\tI1\tsource\t","\tI1\tzero\t"))
    with pytest.raises(ValidationError,match="pin bindings"):
        confirm_selected_constant_sources(mapped,functions,proof,observed,inventory,**options)


@pytest.mark.parametrize("chain,match",[("dynamic","supported conditional constant state"),("output","output port")])
def test_state_component_cannot_hide_nonconstant_successor_or_output(tmp_path,chain,match):
    from emuflow.vivado_constant_frontier import prepare_constant_frontier_query
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,state_chain=chain)
    with pytest.raises(ValidationError,match=match):
        prepare_constant_frontier_query(mapped,proof,inventory,mapped_sha256=digest,
            candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,
            fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])


def test_internal_state_must_be_absent_from_the_native_snapshot(tmp_path):
    import csv
    import json
    import sqlite3
    from emuflow.vivado_constant_frontier import prepare_constant_frontier_query
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,state_chain="pipeline")
    with inventory.open("a") as stream:
        csv.writer(stream,delimiter="\t",lineterminator="\n").writerow(["p/relay","FDRE",f"s{origins['relay']}",""])
    # Bind this altered observation owner, so failure exercises the internal
    # state's actual presence rather than a mismatched inventory seal.
    with sqlite3.connect(proof) as db:
        db.execute("UPDATE metadata SET value=? WHERE key='stage_inventory_sha256'",(json.dumps(file_sha256(inventory)),))
    with pytest.raises(ValidationError,match="still implemented"):
        prepare_constant_frontier_query(mapped,proof,inventory,mapped_sha256=digest,
            candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,
            fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])


def test_required_initialization_dependency_cannot_hide_an_unchecked_output(tmp_path):
    from emuflow.vivado_constant_frontier import prepare_constant_frontier_query
    from emuflow.io import file_sha256
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,dependent=True,dependency_output=True)
    with pytest.raises(ValidationError,match="output port"):
        prepare_constant_frontier_query(mapped,proof,inventory,mapped_sha256=digest,
            candidate_proofs_sha256=file_sha256(proof),checkpoint_sha256="a"*64,
            fpga="fpga0",selected_sources=[f"s{origins['conditional']}"])
