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
