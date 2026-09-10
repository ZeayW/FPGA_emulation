import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_path_binding import iter_snapshot_path_bindings
from test_snapshot_rounds import chain


def database(ir, paths):
    def ep(port): return dict(object='q/'+port, instance='q', port=port, bit=0)
    return dict(schema='emuflow.sta-path-database/v1', design=ir.value['design']['name'],
        source={'provider':'opensta-fpga-path-database-v1'},
        normalization=dict(positive_slack_scale_ns=1.0, negative_slack_scale_ns=1.0, max_clock_period_ns=40.0),
        paths=[dict(id=name, clock_domain='clk', clock_period_ns=40.0, slack_ns=0.0,
            fixed_delay_ns=40.0, normalized_slack=0.0, path_nets=nets,
            startpoint=ep('Q'),endpoint=ep('D')) for name,nets in paths])


def test_cross_and_return_preserves_order_and_member_id():
    ir=chain();db=database(ir,[('original-member',['first','second','third'])])
    rows=list(iter_snapshot_path_bindings(db,ir,{'q':'board0','x':'board1','y':'board0'},port_owners={}))
    assert rows[0]['path']=='original-member'
    segments=rows[0]['segments']
    assert [s['board'] for s in segments]==['board0','board1','board0']
    assert [s['outgoing_cut'] for s in segments]==['first','second',None]
    assert [(s['net_begin'],s['net_end']) for s in segments]==[(0,0),(0,1),(1,2)]


def test_reconvergent_members_not_collapsed_and_off_path_multicast_not_added():
    ir=chain()
    ir.value['instances'].append(dict(id='other',type='LUT1',parameters={'INIT':'01'},resources={'lut':1}))
    ir.value['instances'][2].update(type='LUT2',parameters={'INIT':'1110'})
    ir.value['nets'][0]['sinks'].append(dict(instance='other',port='I0',bit=0))
    ir.value['nets'].append(dict(id='alternate',cut_class='combinational',
        drivers=[dict(instance='other',port='O',bit=0)],sinks=[dict(instance='y',port='I1',bit=0)]))
    db=database(ir,[('crossing',['first','second','third']),('local',['first','alternate','third'])])
    rows=list(iter_snapshot_path_bindings(db,ir,dict(q='board0',x='board1',y='board0',other='board0'),port_owners={}))
    assert [r['path'] for r in rows]==['crossing','local']
    assert [len(r['segments']) for r in rows]==[3,1]
    assert rows[1]['segments'][0]['outgoing_cut'] is None


def test_reordered_or_skipped_nets_and_endpoint_mismatch_fail():
    ir=chain();base=database(ir,[('path',['first','second','third'])])
    for nets in (['third','second','first'],['first','third']):
        db=copy.deepcopy(base);db['paths'][0]['path_nets']=nets
        with pytest.raises(ValidationError):
            list(iter_snapshot_path_bindings(db,ir,dict(q='board0',x='board1',y='board0'),port_owners={}))
    db=copy.deepcopy(base);db['paths'][0]['endpoint']['port']='Q'
    with pytest.raises(ValidationError,match='mapped FF/host'):
        list(iter_snapshot_path_bindings(db,ir,dict(q='board0',x='board1',y='board0'),port_owners={}))
