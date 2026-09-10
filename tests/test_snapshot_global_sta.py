from unittest.mock import patch
import shutil

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_global_sta import build_snapshot_global_checks, run_snapshot_global_checks
from emuflow.snapshot_path_events import iter_snapshot_path_events
from emuflow.snapshot_timing_population import boundary_id
from test_snapshot_path_events import fixture


def checks():
    paths,pair,protocol=fixture()
    events=list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={boundary_id('state','q'):1}))
    timing={(s['board'],s['launch'],s['capture']):dict(delay_ns=d,setup_ns=1)
            for s,d in zip(paths[0]['segments'],(25,2,3))}
    database={'paths':[{'id':'p','clock_period_ns':40,'slack_ns':9999,'fixed_delay_ns':9999}]}
    return database,paths,events,timing


def test_original_observations_keep_readiness_and_ignore_frontend_numbers():
    rows=build_snapshot_global_checks(*checks(),cycle=0,setup_uncertainty_ns=0)
    assert len(rows)==5
    assert [r.role for r in rows]==['tx','tx','commit','target','runtime']
    assert rows[0].arcs_ns==(25,)
    assert rows[0].required_ns==18
    assert rows[3].launch_ns==rows[4].launch_ns==89
    assert rows[3].arcs_ns==rows[4].arcs_ns==(3,)
    assert rows[3].required_ns==39
    assert rows[4].required_ns==98


def test_missing_physical_or_original_member_cannot_be_dropped():
    db,paths,events,timing=checks()
    with pytest.raises(ValidationError,match='no zero-delay'):
        build_snapshot_global_checks(db,paths,events,{},cycle=0,setup_uncertainty_ns=0)
    with pytest.raises(ValidationError,match='exactly once'):
        build_snapshot_global_checks(db,paths,[],timing,cycle=0,setup_uncertainty_ns=0)
    with pytest.raises(ValidationError,match='duplicate'):
        build_snapshot_global_checks(db,paths,events+events,timing,cycle=0,setup_uncertainty_ns=0)


def test_numeric_execution_does_not_call_python_arc_replay(tmp_path):
    rows=build_snapshot_global_checks(*checks(),cycle=0,setup_uncertainty_ns=0)
    with patch('emuflow.snapshot_global_sta.run_event_checks',return_value=[]) as native:
        run_snapshot_global_checks(rows,tmp_path,sta='sta')
    assert native.call_args.kwargs['verify_arcs'] is False


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_global_target_runtime_and_early_readiness_are_distinct(tmp_path):
    rows=build_snapshot_global_checks(*checks(),cycle=0,setup_uncertainty_ns=0)
    result=run_snapshot_global_checks(rows,tmp_path,sta=shutil.which('sta'))
    by={(r['role'],r['event']):r for r in result}
    assert by['tx','segment-0']['slack_ns']==pytest.approx(-7,abs=.001)
    assert by['target','capture']['slack_ns']==pytest.approx(-53,abs=.001)
    assert by['runtime','capture']['slack_ns']==pytest.approx(6,abs=.001)
