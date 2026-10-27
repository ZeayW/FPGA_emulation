"""Source functions must preserve truth tables and real timing coverage."""
import json
import os
import random
import re
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.indexed_timing import IndexedTimingView
from emuflow.opensta import DEFAULT_TIMING_MODEL, load_timing_model, render_opensta_liberty
from emuflow.sta_lut_functions import lut_output_functions
from emuflow.yosys import import_yosys_json
from tests.native_build import emuflow_native


@pytest.mark.parametrize('width', range(1, 7))
def test_lut_boolean_functions_match_the_entire_original_truth_table(width):
    generator = random.Random(width)
    inits = [0, (1 << (1 << width))-1, *(generator.getrandbits(1 << width) for _ in range(15))]
    for init in inits:
        functions = lut_output_functions('LUT'+str(width), init)
        for index in range(1 << width):
            expression = re.sub(r'I([0-5])', lambda m: str(bool(index & (1 << int(m[1])))), functions['O'])
            expression = expression.replace('!', ' not ').replace('&', ' and ').replace('|', ' or ')
            assert bool(eval(expression.strip(), {'__builtins__': {}}, {})) == bool(init & (1 << index))


def test_dual_output_lut_does_not_use_the_upper_half_for_o5():
    assert lut_output_functions('LUT6_2', 0xffffffff00000000)['O5'] == '0'
    assert lut_output_functions('LUT6_2', 0x00000000ffffffff)['O5'] == '1'


@pytest.mark.parametrize('value', [True, -1, 16, '10x0', None, ''])
def test_invalid_lut_init_never_becomes_a_constant(value):
    with pytest.raises(ValidationError): lut_output_functions('LUT2', value)


def make_source(tmp_path):
    raw = dict(modules=dict(top=dict(attributes=dict(top='1'), ports={
        'clk': dict(direction='input', bits=[2]), 'data': dict(direction='input', bits=[3]),
        'q': dict(direction='output', bits=[6])}, netnames={}, cells={
        'constant_gate': dict(type='LUT2', parameters=dict(INIT='1000'), attributes={},
            connections=dict(I0=['0'], I1=[3], O=[4]), port_directions=dict(I0='input', I1='input', O='output')),
        'capture': dict(type='FDRE', parameters={}, attributes={},
            connections=dict(C=[2], D=[4], CE=['1'], R=['0'], Q=[6]),
            port_directions=dict(C='input', D='input', CE='input', R='input', Q='output'))})))
    path = tmp_path/'source.json'; path.write_text(json.dumps(raw))
    source = tmp_path/'source.emuir.sqlite'
    import_yosys_json(path, top='top', clocks=['clk']).save(source)
    return source


def test_native_catalog_preserves_occurrences_and_rejects_unknown_init(tmp_path, monkeypatch):
    from tests.test_native_schedule_generation import install_runtime
    install_runtime(tmp_path, monkeypatch)
    source = make_source(tmp_path)
    view = IndexedTimingView(source)
    try:
        model = load_timing_model(DEFAULT_TIMING_MODEL)
        view.enable_source_lut_functions(model)
        name = view.timing_types()['constant_gate']
        assert name == 'LUT2__SOURCE_INIT_1000'
        assert model['cells'][name]['output_functions']['O'] == '(I0 & I1)'
        assert 'function : "(I0 & I1)";' in render_opensta_liberty(model)
        emitted = '\n'.join(view.timing_verilog_lines(view.timing_types()))
        assert name in emitted
    finally:
        view.close()
    # The native stage must reject a malformed truth table before replacing
    # an existing result; this is not a permissive constant-case waiver.
    with sqlite3.connect(source) as db:
        seq = db.execute("SELECT parameters_seq FROM cell_records WHERE id='constant_gate'").fetchone()[0]
        db.execute('UPDATE payloads SET value=? WHERE seq=?', ('{"INIT":"10x0"}', seq))
    output = tmp_path/'catalog.sqlite'; output.write_bytes(b'previous result')
    result = subprocess.run([str(emuflow_native()), 'phase2', 'build-lut-models', str(source), str(output)],
                            text=True, capture_output=True)
    assert result.returncode != 0 and output.read_bytes() == b'previous result'


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'), reason='qualified OpenSTA required')
def test_real_engine_recognizes_constant_lut_data_without_suppressing_source_checks(tmp_path, monkeypatch):
    from emuflow.opensta import run_opensta_path_database
    from tests.test_native_schedule_generation import install_runtime
    install_runtime(tmp_path, monkeypatch)
    source = make_source(tmp_path)
    # Data is a real external input with a declared arrival; only the AND's
    # tied zero makes its D endpoint constant. CE/R and clock remain checked.
    output = tmp_path/'paths.json'; log = tmp_path/'sta.log'
    run_opensta_path_database(source, output, clocks={'clk': 10.},
        timing_io=dict(input_groups=[dict(clock='clk', delay_ns=0., ports=['data'])],
                       output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
        _stream_output=True, clock_budget_policy='engine-edge-v1', log_path=log)
    assert 'EMUFLOW_SOURCE_TIMING_COVERAGE status=pass' in log.read_text()
