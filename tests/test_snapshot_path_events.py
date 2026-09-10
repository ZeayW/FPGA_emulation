import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_path_binding import iter_snapshot_path_bindings
from emuflow.snapshot_path_events import iter_snapshot_path_events
from emuflow.snapshot_timing_population import boundary_id
from test_snapshot_rounds import chain
from test_snapshot_path_binding import database


def fixture():
    ir=chain()
    paths=list(iter_snapshot_path_bindings(database(ir,[('p',['first','second','third'])]),
        ir,dict(q='board0',x='board1',y='board0'),port_owners={}))
    pair={'boards':{board:{'interface':{'exported_nets':[out], 'imported_nets':[incoming]}}
        for board,out,incoming in [('board0','first','second'),('board1','second','first')]}}
    transfers=[]
    for source,target,epoch,capture,update in [('board0','board1',0,20,40),('board1','board0',0,21,50),
            ('board0','board1',1,70,80),('board1','board0',1,71,90)]:
        transfers.append(dict(source=source,target=target,epoch=epoch,word=0,capture_ns=capture,update_ns=update))
    protocol=dict(scope='simulated_protocol_events_not_measured_link',reset_release_ns={'board0':0,'board1':0},
        cycles=[dict(cycle=0,host_latch_ns=10,commits_ns={'board0':100,'board1':95},transfers=transfers)])
    return paths,pair,protocol


def test_path_uses_causal_earlier_round_not_final_round_for_every_cut():
    paths,pair,protocol=fixture()
    rows=list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={boundary_id('state','q'):1}))
    events=rows[0]['segment_events']
    assert [e['transfer_epoch'] for e in events]==[0,1,None]
    assert [e['launch_ns'] for e in events]==[1,40,90]
    assert [e['capture_ns'] for e in events]==[20,71,100]


def test_completed_commit_is_not_enough_when_shadow_is_stale():
    paths,pair,protocol=fixture()
    protocol['cycles'][0]['transfers']=protocol['cycles'][0]['transfers'][:2]
    with pytest.raises(ValidationError,match='stale or unavailable'):
        list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={boundary_id('state','q'):1}))


def test_next_macrocycle_uses_previous_commit_not_initial_state():
    paths,pair,protocol=fixture()
    second=copy.deepcopy(protocol['cycles'][0]);second.update(cycle=1,host_latch_ns=210,commits_ns={'board0':300,'board1':295})
    for row in second['transfers']:
        row['epoch']+=2;row['capture_ns']+=200;row['update_ns']+=200
    protocol['cycles'].append(second)
    rows=list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={boundary_id('state','q'):1}))
    assert rows[1]['segment_events'][0]['launch_ns']==100


def test_wrong_wire_bit_and_missing_initial_state_fail():
    paths,pair,protocol=fixture()
    with pytest.raises(ValidationError,match='initial state'):
        list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={}))
    pair['boards']['board1']['interface']['imported_nets']=['padding','first']
    with pytest.raises(ValidationError,match='wire bit identity'):
        list(iter_snapshot_path_events(paths,pair,protocol,initial_launch_ns={boundary_id('state','q'):1}))
