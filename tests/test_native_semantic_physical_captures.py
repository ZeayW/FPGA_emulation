"""Complete cross captures use routed costs without a selected path library."""
from contextlib import closing
from copy import deepcopy
import json
import shutil
import sqlite3

import pytest

from tests.test_native_word_physical_binding import physical, eliminated_fanin, replace_semantic, run


@pytest.fixture
def captures(physical):
    config, _ = physical
    contract = config['_fixture_contract']
    contract['capture_requirements'][0].update(endpoint='negative\\雪/hier', port='D', bit=0)
    top = deepcopy(contract['capture_requirements'][0])
    top.update(id='capture000001', kind='top-output', endpoint='top:q[0]')
    top.pop('port'); top.pop('bit')
    contract['capture_requirements'].append(top)
    contract['logic_segments'].append(dict(id='segment000003', kind='rx_to_capture',
        fpga='fpga0', source_cut_net='net1', capture_requirement='capture000001'))
    contract['cut_nodes'][1]['capture_segment_ids'].append('segment000003')
    replace_semantic(physical)
    with closing(sqlite3.connect(config['partitions'][0]['logic'])) as db, db:
        for number, end, kind, data, sampling in (
            (2, 'negative\\\\雪/hier/D', 'pin', 3.25, 4.75),
            (3, 'q[0]', 'port', 1.5, -.25),
        ):
            group = f'segment{number:06}'
            db.execute('INSERT INTO segments(seq,segment_id,kind,system_path,member_path,cut_index,fpga,'
                'start_pin,end_pin,static_exact_segment_id,start_object_kind,end_object_kind,measurement,delay_ns,sampling_cost_ns) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (100+number, f'capture-{number}', 'capture', 'static-exact-contract:'+group,
                 'unselected-'+group, 0, 'fpga0', 'shadow2/Q', end, group, 'pin', kind,
                 'endpoint-exact', data, sampling))
    return physical


def test_all_cross_captures_keep_distinct_routed_costs_and_the_final_rx(captures):
    # The fixture has no original-path selector/local timing store at all.
    result = run(captures, 'captures')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(captures=2, measured=2, eliminated=0,
        records=[[0, 2, 0, 2, 3.25, 4.75], [1, 3, 0, 2, 1.5, -.25]])


def test_capture_cost_binding_is_unchanged_by_continuous_calendar_rebasing(captures):
    before = run(captures, 'captures')
    captures[0].update(capture_edge_ns=50., continuous_phase_word=1,
        continuous_offset_ns=2.5, horizon_words=1000)
    after = run(captures, 'captures')
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert before.stdout == after.stdout


def test_removed_capture_keeps_its_disposition_without_a_numerical_delay(captures, eliminated_fanin):
    config, _ = captures
    config['_fixture_contract']['capture_requirements'][0]['endpoint'] = 'dead'
    replace_semantic(captures)
    with closing(sqlite3.connect(config['proof_context']['inputs'][1]['path'])) as mapped:
        origin = mapped.execute("SELECT seq FROM cells WHERE id='dead'").fetchone()[0]
    with closing(sqlite3.connect(config['partitions'][0]['logic'])) as db, db:
        db.execute("UPDATE segments SET end_pin='dead/D',measurement='optimized-away',delay_ns=NULL,"
            "sampling_cost_ns=NULL,optimization_source=? WHERE segment_id='capture-2'", (f's{origin}',))
    result = run(captures, 'captures')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(captures=2, measured=1, eliminated=1,
        records=[[0, 2, 0, 2, None, None], [1, 3, 0, 2, 1.5, -.25]])


@pytest.mark.parametrize('damage', ['missing', 'extra', 'duplicate', 'different-shadow',
    'different-end', 'different-kind', 'missing-cost', 'elimination-without-owner'])
def test_capture_rows_cannot_be_substituted_or_silently_omitted(captures, damage):
    path = captures[0]['partitions'][0]['logic']
    statements = {
        'missing': "DELETE FROM segments WHERE segment_id='capture-2'",
        'extra': "INSERT INTO segments SELECT seq+1000,segment_id||'-extra',kind,system_path||'-extra',member_path||'-extra',cut_index,fpga,replace_tx_endpoint,start_pin,end_pin,delay_ns,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,measurement,actual_start_object,actual_end_object,optimization_source,sampling_cost_ns FROM segments WHERE segment_id='capture-2'",
        'duplicate': "INSERT INTO segments SELECT seq+1000,segment_id||'-copy',kind,system_path,member_path||'-copy',cut_index+1,fpga,replace_tx_endpoint,start_pin,end_pin,delay_ns,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,measurement,actual_start_object,actual_end_object,optimization_source,sampling_cost_ns FROM segments WHERE segment_id='capture-2'",
        'different-shadow': "UPDATE segments SET start_pin='shadow1/Q' WHERE segment_id='capture-2'",
        'different-end': "UPDATE segments SET end_pin='positive/D' WHERE segment_id='capture-2'",
        'different-kind': "UPDATE segments SET end_object_kind='port' WHERE segment_id='capture-2'",
        'missing-cost': "UPDATE segments SET sampling_cost_ns=NULL WHERE segment_id='capture-2'",
        'elimination-without-owner': "UPDATE segments SET measurement='optimized-away',delay_ns=NULL,sampling_cost_ns=NULL,optimization_source='s0' WHERE segment_id='capture-2'",
    }
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(statements[damage])
    result = run(captures, 'captures')
    assert result.returncode != 0 and not result.stdout, (damage, result.stdout)


def test_capture_consumer_keeps_its_inputs_bound_to_the_same_word_implementation(captures, tmp_path):
    config, _ = captures
    source = tmp_path/'copied-source.sqlite'
    shutil.copyfile(config['source'], source);config['other_source'] = str(source)
    result = run(captures, 'captures-mix-source')
    assert result.returncode != 0 and 'not owned' in result.stderr
    result = run(captures, 'captures-mutate')
    assert result.returncode != 0 and 'changed' in result.stderr
