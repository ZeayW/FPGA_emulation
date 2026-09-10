from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_segment_timing import measure_snapshot_segment_bounds


def run(tmp_path, missing=False):
    q=('q','Q');d=('ff','DI');ce=('ff','CE')
    def capture(setup):return dict(kind='ff',setuphold={edge:((0,0,setup),(0,0,0)) for edge in ('posedge','negedge')})
    graph=dict(roots={q:{}},captures={d:capture(.1),ce:capture(.5)},dynamic_nodes={q,d,ce},order=[q,d,ce],
        edges=[(q,d,None),(q,ce,None)])
    connections=[('q','ff',q,(d,ce),'data'),('ff','ff',('ff','Q'),(),'state_hold'),
                 ('other','ff',('other','Q'),(),'unexplained')]
    def native(cone,directory,**kwargs):
        assert kwargs['launches_ns']=={q:0}
        return {} if missing else {(q,d):dict(arrival_ns=5), (q,ce):dict(arrival_ns=2)}
    with patch('emuflow.snapshot_segment_timing.iter_bound_snapshot_connections',return_value=iter(connections)), \
         patch('emuflow.snapshot_segment_timing.run_ecp5_pair_checks',side_effect=native):
        return measure_snapshot_segment_bounds({'board':'board0'},{},graph,output_dir=tmp_path,sta='sta')


def test_native_bounds_keep_every_target_and_no_fake_untimed_arc(tmp_path):
    result=run(tmp_path)
    assert result['timed']['board0','q','ff']['delay_ns']==5
    assert result['timed']['board0','q','ff']['setup_ns']==.5
    assert result['timed']['board0','q','ff']['physical_target_count']==2
    assert result['untimed']=={('board0','ff','ff'):'state_hold',('board0','other','ff'):'unexplained'}
    assert result['source_relations']==3
    assert not result['global_timing_qualified']


def test_incomplete_native_measurements_fail(tmp_path):
    with pytest.raises(ValidationError,match='coverage mismatch'):run(tmp_path,missing=True)
