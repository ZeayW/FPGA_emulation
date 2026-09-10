import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_source_paths import build_snapshot_source_paths, validate_snapshot_source_paths
from emuflow.snapshot_path_coverage import qualify_snapshot_path_coverage
from emuflow.snapshot_path_binding import iter_snapshot_path_bindings
from test_snapshot_path_coverage import reconvergent


def source():
    ir = reconvergent()
    ir.value['ports'] += [dict(id='clk', direction='input', width=1),
                         dict(id='out', direction='output', width=1)]
    ir.value['nets'].append(dict(id='clock', cut_class='clock',
        drivers=[dict(instance=None, port='clk', bit=0)],
        sinks=[dict(instance='q', port='C', bit=0)]))
    ir.value['nets'][2]['sinks'].append(dict(instance=None,port='out',bit=0))
    return ir


def produce(ir, **options):
    return build_snapshot_source_paths(ir, port_owners={'out':'board0'}, clock_port='clk',
        target_period_ns=40.0, **options)


def test_complete_reconvergent_and_output_population_binds():
    ir = source()
    db = produce(ir, max_paths=4)
    assert len(db['paths']) == 4
    assignment = dict(q='board0', x='board1', y='board0', other='board0')
    coverage = qualify_snapshot_path_coverage(db, ir, assignment, port_owners={'out':'board0'})
    assert coverage['original_members'] == 4
    assert [len(row['segments']) for row in iter_snapshot_path_bindings(db,ir,assignment,port_owners={'out':'board0'})].count(3) == 2
    assert all('slack_ns' not in path and 'fixed_delay_ns' not in path for path in db['paths'])
    shuffled = copy.deepcopy(ir)
    shuffled.value['nets'].reverse()
    shuffled.value['instances'].reverse()
    assert produce(shuffled) == db


def test_budget_rejects_not_truncates_and_clock_is_explicit():
    ir = source()
    with pytest.raises(ValidationError, match='4 paths, above budget 3; no paths enumerated'):
        produce(ir, max_paths=3)
    ir.value['nets'][-1]['sinks'] = []
    with pytest.raises(ValidationError, match='every mapped FF'):
        produce(ir)


def test_source_schema_rejects_fake_sta_numbers_and_bad_endpoint():
    ir = source()
    db = produce(ir)
    db['paths'][0]['slack_ns'] = 0.0
    with pytest.raises(ValidationError, match='synthetic STA'):
        validate_snapshot_source_paths(db,ir)
    del db['paths'][0]['slack_ns']
    db['paths'][0]['startpoint']['object'] = 'nonexistent'
    with pytest.raises(ValidationError, match='original pin'):
        validate_snapshot_source_paths(db,ir)


def test_host_input_direct_and_logic_paths_are_not_unconstrained_omissions():
    ir = source()
    ir.value['ports'].append(dict(id='data',direction='input',width=1))
    ir.value['nets'][0]['drivers'] = [dict(instance=None,port='data',bit=0)]
    db = build_snapshot_source_paths(ir,port_owners={'out':'board0','data':'board0'},
        clock_port='clk',target_period_ns=40.0)
    assert len(db['paths']) == 4
    assert all(path['startpoint']['instance'] is None for path in db['paths'])
