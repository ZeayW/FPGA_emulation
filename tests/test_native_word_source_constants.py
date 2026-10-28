"""Static native pins need actual constant nets and the selected primitive mode."""
import pytest

from emuflow.errors import ValidationError
from emuflow.native_word_source_constants import read_native_source_constants


def facts(path, *, ref, outputs, parameters=None, inputs=None, output_types=None):
    rows = []
    def put(kind, name, prop, value):
        rows.append('\t'.join((kind, name.encode().hex(), prop, str(value).encode().hex())) + '\n')
    put('cell', 'native', 'matches', 1)
    put('cell', 'native', 'REF_NAME', ref)
    for prop, value in (parameters or {}).items():
        put('cell', 'native', prop, value)
    for pin, value in (inputs or {}).items():
        name = 'native/' + pin
        put('pin', name, 'REF_PIN_NAME', pin)
        put('pin', name, 'DIRECTION', 'IN')
        put('connection', name, 'net_names', 'net-' + pin)
        put('net', 'net-' + pin, 'TYPE', 'GROUND' if value == 0 else 'POWER' if value == 1 else 'SIGNAL')
    for pin in outputs:
        name = 'native/' + pin
        put('query', name, 'pin_matches', 1)
        put('pin', name, 'REF_PIN_NAME', pin)
        put('pin', name, 'DIRECTION', 'OUT')
        put('connection', name, 'net_names', 'output-' + pin)
        put('net', 'output-' + pin, 'TYPE', (output_types or {}).get(pin, 'SIGNAL'))
    path.write_text('kind\tobject_hex\tproperty\tvalue_hex\n' + ''.join(rows))
    return read_native_source_constants(path)


@pytest.mark.parametrize('kind,value', [('GROUND', 0), ('POWER', 1)])
def test_native_constant_net_needs_no_original_identity_or_primitive_match(tmp_path, kind, value):
    result, requested = facts(tmp_path / 'facts.tsv', ref='optimized_native_cell', outputs=['O'], output_types={'O': kind})
    assert requested == {'native/O'}
    assert result == {'native/O': {'value': value, 'method': 'native-constant-net'}}


@pytest.mark.parametrize('bus,mode,bit,value', [('A', 'DIRECT', 21, 0), ('B', 'CASCADE', 17, 1)])
def test_unregistered_dsp_padding_follows_actual_selected_input(tmp_path, bus, mode, bit, value):
    selected = (bus if mode == 'DIRECT' else bus + 'CIN') + f'[{bit}]'
    pin = f'{bus}2_DATA[{bit}]'
    result, _ = facts(tmp_path / 'facts.tsv', ref='DSP_A_B_DATA', outputs=[pin],
                      parameters={bus + 'REG': '0', bus + '_INPUT': mode}, inputs={selected: value})
    proof = result['native/' + pin]
    assert proof['value'] == value and proof['input_pin'] == 'native/' + selected
    assert 'delay' not in str(proof)


@pytest.mark.parametrize('damage', ['registered', 'dynamic', 'different-selection', 'unsupported-mode', 'outside-bus'])
def test_dsp_outputs_are_not_assumed_constant_or_registered_from_their_names(tmp_path, damage):
    parameters = {'AREG': '1' if damage == 'registered' else '0',
                  'A_INPUT': 'CASCADE' if damage == 'different-selection' else 'OTHER' if damage == 'unsupported-mode' else 'DIRECT'}
    result, requested = facts(tmp_path / 'facts.tsv', ref='DSP_A_B_DATA',
                              outputs=['A2_DATA[27]' if damage == 'outside-bus' else 'A2_DATA[21]'],
                              parameters=parameters, inputs={'A[21]': None if damage == 'dynamic' else 0, 'A[27]': 0})
    assert len(requested) == 1 and result == {}


def test_carry_select_masks_unknown_data_and_dual_mode_restarts_at_upper_half(tmp_path):
    inputs = {'CI': 1, 'CI_TOP': 0, **{f'S[{i}]': 1 for i in range(8)}}
    outputs = ['O[3]', 'O[4]', 'O[7]', 'CO[7]']
    dual, _ = facts(tmp_path / 'dual.tsv', ref='CARRY8', outputs=outputs,
                    parameters={'CARRY_TYPE': 'DUAL_CY4'}, inputs=inputs)
    single, _ = facts(tmp_path / 'single.tsv', ref='CARRY8', outputs=outputs,
                      parameters={'CARRY_TYPE': 'SINGLE_CY8'}, inputs=inputs)
    assert [dual['native/' + pin]['value'] for pin in outputs] == [0, 1, 1, 0]
    assert [single['native/' + pin]['value'] for pin in outputs] == [0, 0, 0, 1]


def test_carry_equal_mux_arms_remain_constant_but_dynamic_sum_stays_unknown(tmp_path):
    result, requested = facts(tmp_path / 'facts.tsv', ref='CARRY8', outputs=['O[0]', 'O[1]', 'CO[0]'],
                              parameters={'CARRY_TYPE': 'SINGLE_CY8'}, inputs={'CI': 1, 'DI[0]': 1, 'S[1]': 0})
    assert requested == {'native/O[0]', 'native/O[1]', 'native/CO[0]'}
    assert set(result) == {'native/O[1]', 'native/CO[0]'}
    assert all(proof['value'] == 1 for proof in result.values())


def test_conflicting_native_properties_cannot_confirm_a_constant(tmp_path):
    path = tmp_path / 'facts.tsv'
    facts(path, ref='cell', outputs=['O'], output_types={'O': 'GROUND'})
    path.write_text(path.read_text() + 'net\t' + b'output-O'.hex() + '\tTYPE\t' + b'POWER'.hex() + '\n')
    with pytest.raises(ValidationError):
        read_native_source_constants(path)
