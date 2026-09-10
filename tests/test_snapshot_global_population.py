from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_global_population import qualify_snapshot_global_reachability
from emuflow.snapshot_timing_population import boundary_id, build_snapshot_timing_population
from test_snapshot_netlist import fixture
from test_snapshot_rounds import chain


def test_unsplit_reference_matches_split_and_keeps_local_outputs():
    result = qualify_snapshot_global_reachability(fixture(),
        {'a':'board0','b':'board1','inv':'board1'}, port_owners={'result':'board0'})
    assert result['endpoint_pairs'] == 3
    assert result['crossing_endpoint_pairs'] == 2
    assert result['cut_bridges'] == 2
    assert result['crossing_capture_masks'][boundary_id('host','result')] == 0
    assert not result['original_path_tns_qualified']
    assert not result['global_timing_qualified']


def test_same_board_endpoints_may_still_cross_and_return():
    result = qualify_snapshot_global_reachability(chain(),
        {'q':'board0','x':'board1','y':'board0'}, port_owners={})
    assert result['endpoint_pairs'] == result['crossing_endpoint_pairs'] == 1
    assert result['cut_bridges'] == 2
    # No bridge is inferred just from endpoint ownership.
    local = qualify_snapshot_global_reachability(chain(),
        {'q':'board0','x':'board0','y':'board0'}, port_owners={})
    assert local['endpoint_pairs'] == 1
    assert local['crossing_endpoint_pairs'] == local['cut_bridges'] == 0


def test_missing_split_path_fails_against_unsplit_source():
    def broken(*args, **kwargs):
        result = build_snapshot_timing_population(*args, **kwargs)
        if kwargs['board'] == 'board1':
            result['capture_masks'][boundary_id('state','b')] = 0
        return result
    with patch('emuflow.snapshot_global_population.build_snapshot_timing_population', side_effect=broken):
        with pytest.raises(ValidationError, match='changes original endpoint'):
            qualify_snapshot_global_reachability(fixture(),
                {'a':'board0','b':'board1','inv':'board1'}, port_owners={'result':'board0'})


def test_state_capture_does_not_feed_same_cycle_launch():
    result = qualify_snapshot_global_reachability(fixture(),
        {'a':'board0','b':'board1','inv':'board1'}, port_owners={'result':'board0'})
    for state in ('a','b'):
        key = boundary_id('state',state)
        own = 1 << result['launch_labels'].index(key)
        assert not result['capture_masks'][key] & own
