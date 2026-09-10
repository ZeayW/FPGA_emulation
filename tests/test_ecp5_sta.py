from pathlib import Path
import shutil
import pytest
from emuflow.ecp5_sta import export_ecp5_data_checks, run_ecp5_data_checks, run_ecp5_pair_checks
from emuflow.errors import ValidationError
from emuflow.ecp5_data_graph import select_ecp5_data_cones
from emuflow.ecp5_sta import run_ecp5_event_setup_checks


def graph():
    a=('a','Q'); b=('b','Q'); c=('lut','F'); d=('sink','DI')
    def delay(v): return ((v,v,v),(v,v,v))
    return {'roots':{a:{'kind':'ff','clock_to_q':delay(.2)}, b:{'kind':'ff','clock_to_q':delay(.4)}},
            'captures':{d:{'kind':'ff'}},'dynamic_nodes':{a,b,c,d},'order':[a,b,c,d],
            'edges':[(a,c,delay(2)),(b,c,delay(4)),(c,d,delay(.5))]}


def test_cone_selection_preserves_raw_selected_path_and_does_not_mutate():
    g = graph()
    selected = select_ecp5_data_cones(g, roots=[('a','Q')], captures=[('sink','DI')])
    assert selected['edges'] == [g['edges'][0], g['edges'][2]]
    assert set(selected['roots']) == {('a','Q')}
    assert len(g['edges']) == 3
    g['edges'] = []
    with pytest.raises(ValidationError, match='no root-to-capture'):
        select_ecp5_data_cones(g, roots=[('a','Q')], captures=[('sink','DI')])


def test_event_window_offsets_and_setup_are_passed_to_native(monkeypatch, tmp_path):
    g = graph(); a=('a','Q'); z=('sink','DI')
    g['captures'][z]['setuphold'] = {edge: ((.1,.2,.3),(.1,.1,.1)) for edge in ('posedge','negedge')}
    def native(selected, directory, **kwargs):
        assert set(selected['roots']) == {a}
        assert kwargs['launches_ns'] == {a: 0}
        assert kwargs['deadlines_ns'][z] == pytest.approx(9.5)
        assert kwargs['analysis'] == 'max'
        return {(a,z): dict(arrival_ns=2.7, required_ns=9.5, slack_ns=6.8)}
    monkeypatch.setattr('emuflow.ecp5_sta.run_ecp5_pair_checks', native)
    rows = run_ecp5_event_setup_checks(g, tmp_path, pairs=[(a,z)], launch_edges_ns={a: 1e9},
        capture_edges_ns={z: 1e9+10}, setup_uncertainty_ns=.2)
    assert rows[a,z]['arrival_ns'] == pytest.approx(1e9+2.7)
    assert rows[a,z]['slack_ns'] == 6.8
    with pytest.raises(ValidationError, match='complete exact'):
        run_ecp5_event_setup_checks(g, tmp_path, pairs=[(a,z)], launch_edges_ns={},
            capture_edges_ns={z: 10}, setup_uncertainty_ns=0)


@pytest.mark.skipif(not shutil.which('sta'), reason='native OpenSTA unavailable')
def test_native_event_window_preserves_tight_slack_at_large_absolute_epoch(tmp_path):
    g = graph(); a=('a','Q'); z=('sink','DI')
    g['captures'][z]['setuphold'] = {edge: ((.1,.2,.3),(.1,.1,.1)) for edge in ('posedge','negedge')}
    rows = run_ecp5_event_setup_checks(g, tmp_path, pairs=[(a,z)], launch_edges_ns={a: 1e9},
        capture_edges_ns={z: 1e9+3}, setup_uncertainty_ns=.2)
    assert rows[a,z]['arrival_ns'] - 1e9 == pytest.approx(2.7,abs=.001)
    assert rows[a,z]['required_ns'] - 1e9 == pytest.approx(2.5,abs=.001)
    assert rows[a,z]['slack_ns'] == pytest.approx(-.2,abs=.001)


def test_distant_launch_epochs_are_not_combined_in_native_float_constraints(monkeypatch, tmp_path):
    g=graph();a=('a','Q');b=('b','Q');z=('sink','DI')
    g['captures'][z]['setuphold']={edge: ((.1,.2,.3),(.1,.1,.1)) for edge in ('posedge','negedge')}
    seen=[]
    def native(cone, directory, **kwargs):
        seen.append(kwargs)
        return {pair: dict(arrival_ns=0, required_ns=kwargs['deadlines_ns'][z], slack_ns=0)
                for pair in kwargs['pairs']}
    monkeypatch.setattr('emuflow.ecp5_sta.run_ecp5_pair_checks',native)
    run_ecp5_event_setup_checks(g,tmp_path,pairs=[(a,z),(b,z)],
        launch_edges_ns={a:1e9,b:1.1e9},capture_edges_ns={z:1.1e9+5},setup_uncertainty_ns=.2)
    assert len(seen)==2
    assert seen[1]['launches_ns']=={b:0}
    assert seen[1]['deadlines_ns'][z]==pytest.approx(4.5)


def test_export_retains_shared_fanin_and_separate_delay_arcs(tmp_path):
    g=graph(); export_ecp5_data_checks(g,tmp_path,launches_ns={r:0 for r in g['roots']},deadlines_ns={('sink','DI'):10})
    text=(tmp_path/'physical.v').read_text()
    assert 'M2' in text and text.count('wire e')==3
    assert '4.9' not in (tmp_path/'physical.lib').read_text()
    assert '-max -9 ' in (tmp_path/'physical.sdc').read_text()


def test_missing_constraints_fail_before_export(tmp_path):
    with pytest.raises(ValidationError,match='complete'):
        export_ecp5_data_checks(graph(),tmp_path,launches_ns={},deadlines_ns={})
    assert not (tmp_path/'physical.v').exists()


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_opensta_selects_longer_branch(tmp_path):
    g=graph(); measurements=run_ecp5_data_checks(g,tmp_path,launches_ns={r:0 for r in g['roots']},deadlines_ns={('sink','DI'):10})
    row=measurements['sink','DI']
    assert row['arrival_ns']==pytest.approx(4.9,abs=.001)
    assert row['slack_ns']==pytest.approx(5.1,abs=.001)


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_pairs_preserve_noncritical_launch(tmp_path):
    g=graph();end=('sink','DI');pairs=[(r,end) for r in g['roots']]
    result=run_ecp5_pair_checks(g,tmp_path,pairs=pairs,launches_ns={r:0 for r in g['roots']},deadlines_ns={end:10})
    assert result[(('a','Q'),end)]['arrival_ns']==pytest.approx(2.7,abs=.001)
    assert result[(('b','Q'),end)]['arrival_ns']==pytest.approx(4.9,abs=.001)


def test_pair_binding_rejected_before_export(tmp_path):
    with pytest.raises(ValidationError,match='bound dynamic'):
        run_ecp5_pair_checks(graph(),tmp_path,pairs=[(('wrong','Q'),('sink','DI'))],launches_ns={},deadlines_ns={})
    assert not list(tmp_path.iterdir())


def test_min_export_uses_earliest_arcs_and_same_edge(tmp_path):
    g=graph();g['edges'][0]=(g['edges'][0][0],g['edges'][0][1],((.1,1,2),(.3,2,4)))
    export_ecp5_data_checks(g,tmp_path,launches_ns={r:0 for r in g['roots']},deadlines_ns={('sink','DI'):.4},analysis='min')
    assert '-min -0.40000000000000002 ' in (tmp_path/'physical.sdc').read_text()
    assert '-path_delay min' in (tmp_path/'analyze.tcl').read_text()
    assert '-max' not in (tmp_path/'physical.sdc').read_text()


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_min_reports_hold_violation(tmp_path):
    g=graph();end=('sink','DI')
    rows=run_ecp5_pair_checks(g,tmp_path,pairs=[(r,end) for r in g['roots']],
        launches_ns={r:0 for r in g['roots']},deadlines_ns={end:3},analysis='min')
    assert rows[(('a','Q'),end)]['arrival_ns']==pytest.approx(2.7,abs=.001)
    assert rows[(('a','Q'),end)]['slack_ns']==pytest.approx(-.3,abs=.001)
    assert rows[(('b','Q'),end)]['slack_ns']==pytest.approx(1.9,abs=.001)
