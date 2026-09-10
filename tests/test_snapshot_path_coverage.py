import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_path_coverage import qualify_snapshot_path_coverage
from test_snapshot_path_binding import database
from test_snapshot_rounds import chain


def reconvergent():
    ir = chain()
    ir.value['instances'].append(dict(id='other', type='LUT1', parameters={'INIT':'01'}, resources={'lut':1}))
    ir.value['instances'][2].update(type='LUT2', parameters={'INIT':'1110'})
    ir.value['nets'][0]['sinks'].append(dict(instance='other', port='I0', bit=0))
    ir.value['nets'].append(dict(id='alternate', cut_class='combinational',
        drivers=[dict(instance='other', port='O', bit=0)], sinks=[dict(instance='y', port='I1', bit=0)]))
    return ir


def check(ir, db):
    return qualify_snapshot_path_coverage(db, ir,
        {cell['id']: 'board1' if cell['id'] == 'x' else 'board0' for cell in ir.value['instances']},
        port_owners={port['id']: 'board0' for port in ir.value['ports']})


def test_endpoint_complete_but_reconvergent_path_missing_rejected():
    ir = reconvergent()
    db = database(ir, [('only-worst', ['first','second','third'])])
    with pytest.raises(ValidationError, match='expected 2 distinct net chains, received 1'):
        check(ir, db)
    db['paths'].extend(database(ir, [('other-branch', ['first','alternate','third'])])['paths'])
    result = check(ir, db)
    assert result['original_members'] == 2
    assert not result['global_timing_qualified']


def test_duplicate_member_cannot_replace_missing_branch():
    ir = reconvergent()
    db = database(ir, [('one',['first','second','third']), ('two',['first','second','third'])])
    with pytest.raises(ValidationError, match='duplicate original structural net chain'):
        check(ir, db)


def test_host_output_omission_rejected_and_covered():
    ir = chain()
    ir.value['ports'].append(dict(id='out', direction='output', width=1))
    ir.value['nets'][-1]['sinks'].append(dict(instance=None, port='out', bit=0))
    db = database(ir, [('state',['first','second','third'])])
    with pytest.raises(ValidationError, match='expected 2 distinct net chains'):
        check(ir, db)
    output = copy.deepcopy(db['paths'][0])
    output.update(id='output', endpoint=dict(object='out', instance=None, port='out', bit=0))
    db['paths'].append(output)
    assert check(ir, db)['nonconstant_captures'] == 2


def test_tied_inputs_are_one_net_chain_not_duplicate_arc_paths():
    ir = chain()
    ir.value['instances'][1].update(type='LUT2', parameters={'INIT':'0110'})
    ir.value['nets'][0]['sinks'].append(dict(instance='x', port='I1', bit=0))
    # The structure remains a chain even though Boolean cofactoring cancels it.
    # Boolean cancellation is a separate physical-correspondence proof.
    assert check(ir, database(ir, [('p',['first','second','third'])]))['original_members'] == 1


def test_exponential_missing_population_is_counted_without_enumeration():
    ir = chain()
    ir.value['instances'] = ir.value['instances'][:1]
    ir.value['nets'] = []
    previous = dict(instance='q', port='Q', bit=0)
    chosen = []
    for stage in range(30):
        left, right, join = (f'{prefix}{stage}' for prefix in ('l','r','j'))
        for name in (left, right):
            ir.value['instances'].append(dict(id=name, type='LUT1', parameters={'INIT':'01'}, resources={'lut':1}))
        ir.value['instances'].append(dict(id=join, type='LUT2', parameters={'INIT':'1110'}, resources={'lut':1}))
        name = f'n{stage}'
        ir.value['nets'].append(dict(id=name, cut_class='combinational', drivers=[previous],
            sinks=[dict(instance=i, port='I0', bit=0) for i in (left,right)]))
        for branch, pin in ((left,'I0'), (right,'I1')):
            ir.value['nets'].append(dict(id=branch, cut_class='combinational',
                drivers=[dict(instance=branch, port='O', bit=0)],
                sinks=[dict(instance=join, port=pin, bit=0)]))
        chosen.extend((name,left))
        previous = dict(instance=join, port='O', bit=0)
    ir.value['nets'].append(dict(id='final', cut_class='combinational', drivers=[previous],
        sinks=[dict(instance='q', port='D', bit=0)]))
    chosen.append('final')
    with pytest.raises(ValidationError, match='expected 1073741824 distinct net chains, received 1'):
        check(ir, database(ir, [('sample', chosen)]))
