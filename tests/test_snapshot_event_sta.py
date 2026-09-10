from contextlib import ExitStack
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_event_sta import qualify_snapshot_event_setup


def execute(tmp_path, *, connections=None, proofs=None, missing=False):
    rows = connections if connections is not None else [
        ('rx', 'q', ('rx','Q'), (('q','DI'), ('q','CE')), 'data'),
        ('q', 'q', ('q','Q'), (), 'state_hold'),
        ('unused', 'q', ('unused','Q'), (), 'unexplained')]
    proofs = [{'launch':'unused', 'capture':'q'}] if proofs is None else proofs
    with ExitStack() as stack:
        stack.enter_context(patch('emuflow.snapshot_event_sta.build_snapshot_timing_population', return_value={}))
        proof = stack.enter_context(patch('emuflow.snapshot_event_sta.qualify_snapshot_source_correspondence',
            return_value={'sensitivity_proofs':proofs}))
        stack.enter_context(patch('emuflow.snapshot_event_sta.iter_bound_snapshot_connections', return_value=iter(rows)))
        stack.enter_context(patch('emuflow.snapshot_event_sta.iter_snapshot_timing_windows', return_value=iter([
            dict(captures=('q',),launches_ns={'rx':90,'q':1,'unused':1},capture_edge_ns=100,cycle=0,epoch=None)])))
        def native(graph, directory, **kwargs):
            assert kwargs['launch_edges_ns'] == {('rx','Q'):90}
            assert kwargs['capture_edges_ns'] == {('q','DI'):100,('q','CE'):100}
            return {} if missing else {pair:{'slack_ns':-1 if pair[1][1]=='CE' else 3} for pair in kwargs['pairs']}
        stack.enter_context(patch('emuflow.snapshot_event_sta.run_ecp5_event_setup_checks',side_effect=native))
        result = qualify_snapshot_event_setup(None,{},board='board0',port_owners={},interface={},bindings={},
            graph={},protocol={},initial_ready_ns={},setup_uncertainty_ns=.2,output_dir=tmp_path,yosys='yosys',sta='sta')
        proof.assert_called_once()
        return result


def test_all_timed_controls_and_nontimed_relations_accounted(tmp_path):
    result=execute(tmp_path)
    assert result['status']=='setup_violations'
    assert result['negative_pair_occurrences']==1
    row=result['windows'][0]
    assert row['physical_pairs']==2
    assert row['logical_timed_pairs']==1
    assert row['state_hold_relations']==row['boolean_independent_relations']==1
    assert row['minimum_setup_slack_ns']==-1
    assert not result['global_timing_qualified']


def test_missing_source_proof_or_native_pair_is_not_waived(tmp_path):
    with pytest.raises(ValidationError,match='exactly cover'):
        execute(tmp_path,proofs=[])
    with pytest.raises(ValidationError,match='coverage mismatch'):
        execute(tmp_path,missing=True)


def test_merged_root_with_inconsistent_epoch_fails(tmp_path):
    with pytest.raises(ValidationError,match='inconsistent logical event'):
        execute(tmp_path,proofs=[],connections=[
            ('rx','q',('rx','Q'),(('q','DI'),),'data'),
            ('q','q',('rx','Q'),(('q','CE'),),'synchronous_control')])
