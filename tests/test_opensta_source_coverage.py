"""A successful path search must not hide unclocked or unconstrained cones."""
import json
import os

import pytest

from emuflow.errors import EmuFlowError
from emuflow.opensta import run_opensta_path_database
from emuflow.yosys import import_yosys_json
from tests.test_opensta_communication_research import prepare_source


real_engine = pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),
                                reason='qualified real OpenSTA required')


@real_engine
@pytest.mark.parametrize('defect', ['input-arrival', 'output-required', 'capture-clock', 'loop'])
def test_incomplete_source_cannot_replace_an_existing_phase2_selector(tmp_path, defect):
    source = prepare_source(tmp_path)
    timing_io = dict(input_groups=[dict(clock='clk', delay_ns=0., ports=['a', 'b'])],
                     output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])])
    if defect == 'input-arrival':
        timing_io['input_groups'][0]['ports'] = ['a']
    elif defect == 'output-required':
        timing_io['output_groups'] = []
    else:
        path = tmp_path/'source.json'
        value = json.loads(path.read_text())
        design = value['modules']['retime']
        if defect == 'capture-clock':
            design['cells']['capture']['connections']['C'] = [30]
            design['ports']['unbound_clock'] = dict(direction='input', bits=[30])
        else:
            # Keep clocked paths elsewhere: merely finding some paths must
            # not accept the omitted cyclic cone feeding this capture.
            design['cells']['fast']['connections']['I0'] = [16]
        path.write_text(json.dumps(value))
        source = tmp_path/'damaged.emuir.sqlite'
        import_yosys_json(path, top='retime', clocks=['clk']).save(source)
    output = tmp_path/'paths.json'
    previous = b'previous selector must survive a failed source check\n'
    output.write_bytes(previous)
    log = tmp_path/'opensta.log'
    with pytest.raises(EmuFlowError, match='source timing coverage failed'):
        run_opensta_path_database(source, output, clocks={'clk': 10.}, timing_io=timing_io,
            executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
            _stream_output=True, clock_budget_policy='engine-edge-v1', log_path=log)
    assert output.read_bytes() == previous
    text = log.read_text()
    assert 'EMUFLOW_SOURCE_TIMING_ERROR' in text
    assert 'EMUFLOW_OPENSTA_DATABASE status=pass' not in text


@real_engine
def test_complete_source_has_coverage_evidence_before_path_publication(tmp_path):
    source = prepare_source(tmp_path)
    output, log = tmp_path/'paths.json', tmp_path/'opensta.log'
    run_opensta_path_database(source, output, clocks={'clk': 10.}, timing_io=dict(
        input_groups=[dict(clock='clk', delay_ns=0., ports=['a', 'b'])],
        output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
        _stream_output=True, clock_budget_policy='engine-edge-v1', log_path=log)
    assert json.loads(output.read_text())['source']['source_timing_coverage'] == {
        'policy': 'engine-no-clock-unconstrained-loops-v1', 'status': 'pass'}
    text = log.read_text()
    assert text.index('EMUFLOW_SOURCE_TIMING_COVERAGE status=pass') < text.index(
        'EMUFLOW_OPENSTA_DATABASE status=pass')
