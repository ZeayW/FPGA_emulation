"""Independent witness batches preserve exact clocks, deadlines and identity."""
import json
import os

import pytest

from emuflow.errors import ValidationError
from emuflow.global_sta import EventCheck, run_event_checks, read_measurements
from emuflow.io import file_sha256
from emuflow.native_word_verification import run_native_event_checks


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_engine_bounds_cannot_omit_checks(tmp_path, limit):
    with pytest.raises(ValidationError):
        run_native_event_checks([], tmp_path, maximum_checks=limit)


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'), reason='real OpenSTA required')
@pytest.mark.parametrize('limit', [1, 3, 7])
def test_complete_engine_results_equal_unsplit_model_across_bundle_boundaries(tmp_path, limit):
    # Include signed event origins, missed deadlines, zero arcs, repeated
    # equivalent bundles and a path bundle crossing an engine boundary.
    rows = [EventCheck('p', 'tx', 'first', -300., (.125, 2.), -297.5),
            EventCheck('p', 'commit', 'first', -300., (.125, 2.), -298.),
            EventCheck('p', 'target', 'capture', 16., (8., .5, 7.), 18.),
            EventCheck('p', 'runtime', 'capture', 16., (8., .5, 7.), 100.),
            EventCheck('q', 'tx', 'empty', 2.5, (), 2.5),
            EventCheck('r', 'tx', 'first', -300., (.125, 2.), -297.5),
            EventCheck('r', 'commit', 'first', -300., (.125, 2.), -298.),
            EventCheck('s', 'target', 'capture', -.25, (.00025,), 0.)]
    engine = os.environ['EMUFLOW_TEST_OPENSTA']
    reference = run_event_checks(rows, tmp_path / 'whole', engine, require_observation_pairs=False)
    output = tmp_path / 'batched'
    actual = run_native_event_checks(rows, output, engine, maximum_checks=limit)
    replay = read_measurements(output / 'measurements.tsv', rows)
    for index, expected in enumerate(reference):
        for result in (actual[index], replay[index]):
            assert (result['path'], result['role'], result['event']) == (
                expected['path'], expected['role'], expected['event'])
            for field in ('arrival_ns', 'required_ns', 'slack_ns'):
                assert result[field] == pytest.approx(expected[field], abs=1e-6)
    manifest = json.loads((output / 'native-word-batch-manifest.json').read_text())
    assert manifest['checks'] == len(rows)
    position = 0
    for batch in manifest['batches']:
        assert batch['begin_check'] == position
        position = batch['end_check']
        for reference in batch['artifacts'].values():
            assert file_sha256(output / reference['artifact']) == reference['sha256']
    assert position == len(rows)
