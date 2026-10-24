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
