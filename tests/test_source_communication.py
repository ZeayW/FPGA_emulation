"""Source-complete crossing joins, independent of the selected path library."""
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.source_communication import export_source_communication, export_source_metric_weights
from emuflow.store_publication import bind_store_source
from tests.native_build import ROOT, emuflow_native
from tests.test_opensta_communication_research import prepare_source, call_engine, capture_row


def create_db(path, app, version, ddl, metadata):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(f'PRAGMA application_id={app};PRAGMA user_version={version};' + ddl)
        db.executemany('INSERT INTO metadata VALUES(?,?)', ((key, json.dumps(value)) for key, value in metadata.items()))


def seal_cuts(path):
    with closing(sqlite3.connect(path)) as db, db:
        parts = dict(db.execute('SELECT part_index,fpga FROM parts'))
        records = []
        for index, net, source, kind, count, round_, level, depth in db.execute('SELECT * FROM cuts ORDER BY cut_index'):
            record = dict(net=net, cut_class=kind, source_fpgas=[parts[source]],
                sink_fpgas=[parts[p] for p, in db.execute('SELECT sink_part_index FROM cut_sinks WHERE net_id=? ORDER BY sink_part_index', (net,))],
                sink_endpoints=count, dependency_level=level, combinational_dependency_depth=depth,
                predecessor_cut_nets=[n for n, in db.execute('SELECT predecessor_net_id FROM cut_predecessors WHERE cut_index=? ORDER BY ordinal', (index,))])
            if round_ is not None:
                record['transport_round'] = round_
            records.append(record)
        data = ''.join(json.dumps(r, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n' for r in records)
        for key, value in dict(cut_records=len(records), cut_records_schema='emuflow.phase3-cut-records/v1',
                               cut_records_sha256=hashlib.sha256(data.encode()).hexdigest()).items():
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, json.dumps(value)))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    binary = emuflow_native()
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(binary.parent.parent))
    original = prepare_source(tmp_path)
    with closing(sqlite3.connect(original)) as db:
        cells = list(db.execute('SELECT seq,id FROM cell_records ORDER BY seq'))
        nets = [db.execute('SELECT n.id,n.cut_class FROM endpoint_records e JOIN cell_records c ON c.seq=e.instance_seq '
            'JOIN net_records n ON n.seq=e.net_seq WHERE c.id=? AND e.role=0', (cell,)).fetchone() for cell in ['launch_b', 'fast']]
    assignment, schedule = tmp_path / 'assignment.sqlite', tmp_path / 'schedule.sqlite'
    ddl = ''.join((ROOT / 'src/emuflow/data/schema' / name).read_text() for name in
                  ['phase3_assignment_core.sql', 'phase3_assignment_cuts.sql'])
    create_db(assignment, 0x454d5541, 4, ddl, dict(schema='emuflow.phase3-assignment-sqlite/v4',
        design='retime', platform='board', source_instance_count=len(cells), semantic_contract_sha256='a' * 64))
    with closing(sqlite3.connect(assignment)) as db, db:
        db.executemany('INSERT INTO parts VALUES(?,?)', enumerate(['fpga0', 'fpga1', 'fpga2', 'fpga3']))
        db.executemany('INSERT INTO copies VALUES(?,?,?,0)',
            [(seq, 0 if name == 'launch_b' else 1 if name == 'fast' else 2, name) for seq, name in cells])
        for i, (net, kind) in enumerate(nets):
            db.execute('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)', (i, net, i, kind, 1, None, i, i))
            db.execute('INSERT INTO cut_sinks VALUES(?,?,?)', (net, i, i + 1))
        db.execute('INSERT INTO cut_predecessors VALUES(1,0,?)', (nets[0][0],))
    seal_cuts(assignment)
    create_db(schedule, 0x454d5554, 3, (ROOT / 'src/emuflow/data/schema/phase5_schedule.sql').read_text(),
        dict(logical_schema='emuflow.tdm-schedule/v1', header=dict(design='retime', platform='board',
            semantic_contract_sha256='a' * 64, word_event_policy='periodic-lvds-8bit-word-load-to-rx-visible-v2')))
    with closing(sqlite3.connect(schedule)) as db, db:
        for i, (net, _kind) in enumerate(nets):
            db.execute('INSERT INTO schedule_routes VALUES(?,?,?,?)', (i, f'd{i}', net, f'fpga{i}'))
            db.execute('INSERT INTO schedule_route_sinks VALUES(?,0,?)', (i, f'fpga{i+1}'))
        for i, (route, frm, to, ratio, depth) in enumerate([(0, 0, 3, 8, 0), (0, 3, 1, 16, 1), (1, 1, 2, 24, 0)]):
            db.execute('INSERT INTO schedule_route_edges VALUES(?,?,?,?,?)', (route, depth, f'l{i}', f'fpga{frm}', f'fpga{to}'))
            db.execute('INSERT INTO schedule_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (i, f'e{i}', f'd{route}', nets[route][0], depth, f'l{i}', f'fpga{frm}', f'fpga{to}', f'l{i}', i + 1, 0, 0, i + 7, ratio))
    return original, assignment, schedule, tmp_path / 'costs.tsv'


def produce(fixture, **kwargs):
    source, assignment, schedule, output = fixture
    schemas = ('emuir-compact-v2', 'assignment-index-v4', 'tdm-schedule-store-v4')
    bindings = kwargs.pop('source_bindings', [bind_store_source(p, s) for p, s in zip(fixture[:3], schemas)])
    return export_source_communication(source, assignment, schedule, output, design='retime', platform='board',
        semantic_sha256='a' * 64, fixed_delay_ns=30., serial_ui_ns=.625,
        source_bindings=bindings, executable=str(emuflow_native()), **kwargs)


def decoded(path):
    with path.open() as stream:
        return [(bytes.fromhex(r['from_pin_hex']).decode(), bytes.fromhex(r['to_pin_hex']).decode(), float(r.get('extra_delay_ns', r.get('integer_weight'))))
                for r in csv.DictReader(stream, delimiter='\t')]


def produce_metric(fixture, metric):
    schemas = ('emuir-compact-v2', 'assignment-index-v4', 'tdm-schedule-store-v4')
    return export_source_metric_weights(*fixture, design='retime', platform='board',
        semantic_sha256='a' * 64, metric=metric,
        source_bindings=[bind_store_source(p, s) for p, s in zip(fixture[:3], schemas)],
        executable=str(emuflow_native()))


def test_native_net_map_retains_port_aliases_and_rejects_ambiguous_generated_names(fixture):
    from emuflow.sta import write_emuir_net_map
    original, _, _, output = fixture
    result = write_emuir_net_map(original, output)
    with output.open() as stream:
        aliases = {bytes.fromhex(row['mapped_net_hex']).decode():bytes.fromhex(row['emuir_net_hex']).decode()
                   for row in csv.DictReader(stream, delimiter='\t')}
    with closing(sqlite3.connect(original)) as db:
        net, identity = db.execute("SELECT e.net_seq,n.id FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq "
            "JOIN net_records n ON n.seq=e.net_seq WHERE e.instance_seq IS NULL AND p.name='a'").fetchone()
    assert result['port_aliases'] > 0 and aliases['a'] == aliases[f'__emuflow_net_{net}'] == identity
    before = output.read_bytes()
    other = (net + 1) % result['nets']
    name = f'__emuflow_net_{other}'
    with closing(sqlite3.connect(original)) as db, db:
        db.execute("UPDATE port_records SET record_json=json_set(record_json,'$.id',?,'$.name',?) "
                   "WHERE json_extract(record_json,'$.id')='a'", (name,name))
        db.execute("UPDATE pin_names SET name=? WHERE name='a'", (name,))
    with pytest.raises(ValidationError, match='different generated timing wire'):
        write_emuir_net_map(original, output)
    assert output.read_bytes() == before


def test_native_net_map_does_not_renew_an_old_parent_generation(fixture):
    from emuflow.native_protocol import CheckedRequestWriter
    original, _, _, output = fixture
    parent = bind_store_source(original,'emuir-compact-v2')
    request = output.with_suffix('.request')
    writer = CheckedRequestWriter(request)
    try:
        writer.put('QII',0x31514d4e534d5545,1,1)
        writer.put('QQQqqqq',*parent.generation)
        writer.finish()
    finally:
        writer.close()
    output.write_bytes(b'previous')
    os.utime(original,None)
    import subprocess
    result = subprocess.run([str(emuflow_native()),'phase2','export-net-map',str(original),str(request),str(output)],
                            capture_output=True,text=True)
    assert result.returncode != 0 and 'mixed-generation' in result.stderr
    assert output.read_bytes() == b'previous'


@pytest.mark.parametrize('metric,values', [('physical-hops', (2, 1)),
    ('maximum-net-route-ratio', (16, 24)), ('serial-ratio-sum', (24, 24))])
def test_metric_weights_are_explicit_integer_scores_not_communication_delays(fixture, metric, values):
    result = produce_metric(fixture, metric)
    assert result['schema'] == 'emuflow.source-metric-weights/v1'
    assert result['metric'] == metric and result['weight_scale_ns'] == 1000000000
    assert result['interpretation'] == 'integer-path-metric-not-physical-time'
    assert set(decoded(fixture[3])) == {('launch_b/Q', 'fast/I0', values[0]),
                                      ('fast/O', 'join/I1', values[1])}


def test_multicast_metrics_use_only_the_actual_destination_branch(fixture):
    original, assignment, schedule, output = fixture
    with closing(sqlite3.connect(original)) as db, db:
        net = db.execute("SELECT e.net_seq FROM endpoint_records e JOIN cell_records c ON c.seq=e.instance_seq WHERE c.id='launch_b' AND e.role=0").fetchone()[0]
        ordinal = db.execute('SELECT coalesce(max(ordinal)+1,0) FROM endpoint_records WHERE net_seq=? AND role=1', (net,)).fetchone()[0]
        db.execute("UPDATE endpoint_records SET net_seq=?,ordinal=? WHERE instance_seq=(SELECT seq FROM cell_records WHERE id='slow0') AND role=1", (net, ordinal))
    with closing(sqlite3.connect(assignment)) as db, db:
        net_id = db.execute('SELECT net_id FROM cuts WHERE cut_index=0').fetchone()[0]
        db.execute('UPDATE cuts SET sink_endpoints=2 WHERE cut_index=0')
        db.execute('INSERT INTO cut_sinks VALUES(?,0,2)', (net_id,))
    seal_cuts(assignment)
    with closing(sqlite3.connect(schedule)) as db, db:
        db.execute("INSERT INTO schedule_route_sinks VALUES(0,1,'fpga2')")
        db.execute("INSERT INTO schedule_route_edges VALUES(0,2,'l3','fpga0','fpga2')")
        db.execute('INSERT INTO schedule_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (3, 'e3', 'd0', net_id, 0, 'l3', 'fpga0', 'fpga2', 'l3', 4, 0, 0, 10, 32))
    expected = {'physical-hops': (2, 1), 'maximum-net-route-ratio': (16, 32),
                'serial-ratio-sum': (24, 32)}
    for metric, values in expected.items():
        produce_metric(fixture, metric)
        rows = {(a, b): v for a, b, v in decoded(output)}
        assert rows['launch_b/Q', 'fast/I0'] == values[0]
        assert rows['launch_b/Q', 'slow0/I0'] == values[1]


@pytest.mark.parametrize('metric', ['physical-hops', 'maximum-net-route-ratio', 'serial-ratio-sum'])
def test_metric_export_rejects_incomplete_connectivity_without_replacing_its_owner(fixture, metric):
    produce_metric(fixture, metric)
    before = fixture[3].read_bytes()
    with closing(sqlite3.connect(fixture[2])) as db, db:
        db.execute('DELETE FROM schedule_entries WHERE seq=2')
    with pytest.raises(EmuFlowError): produce_metric(fixture, metric)
    assert fixture[3].read_bytes() == before


@pytest.mark.parametrize('metric', [None, False, [], 'delay-ns', 'maximum-ratio'])
def test_metric_kind_must_be_explicit(tmp_path, metric):
    with pytest.raises(ValidationError, match='unsupported source graph metric'):
        export_source_metric_weights(*(tmp_path / str(i) for i in range(4)), design='retime',
            platform='board', semantic_sha256='a' * 64, source_bindings=[], metric=metric)


def test_all_actual_crossings_accumulate_only_their_own_route_branch(fixture):
    with patch('emuflow.ir.EmuIR.load', side_effect=AssertionError('whole graph loaded in Python')):
        result = produce(fixture)
    assert result['connections'] == 2 and result['crossing_nets'] == 2 and result['route_edges'] == 3
    assert set(decoded(fixture[3])) == {('launch_b/Q', 'fast/I0', 75.), ('fast/O', 'join/I1', 45.)}
    assert result['cost_policy'] == 'per-hop-fixed-plus-serial-ui-times-ratio-planning-bound-v1'


@pytest.mark.parametrize('damage', ['missing-entry', 'extra-edge', 'wrong-ratio', 'wrong-depth', 'wrong-owner',
                                   'wrong-copy-name', 'missing-source-sink', 'duplicate-arrival', 'cycle', 'replica'])
def test_incomplete_or_mixed_owners_do_not_replace_a_cost_file(fixture, damage):
    produce(fixture)
    source, assignment, schedule, output = fixture
    before = output.read_bytes()
    with closing(sqlite3.connect(assignment if damage in ['wrong-owner', 'wrong-copy-name', 'missing-source-sink', 'replica'] else schedule)) as db, db:
        if damage == 'missing-entry': db.execute('DELETE FROM schedule_entries WHERE seq=2')
        elif damage == 'extra-edge': db.execute("INSERT INTO schedule_route_edges VALUES(1,1,'extra','fpga2','fpga3')")
        elif damage == 'wrong-ratio': db.execute('UPDATE schedule_entries SET tdm_ratio=9 WHERE seq=0')
        elif damage == 'wrong-depth': db.execute('UPDATE schedule_entries SET hop=0 WHERE seq=1')
        elif damage == 'wrong-owner': db.execute("UPDATE copies SET part_index=0 WHERE copy_id='fast'")
        elif damage == 'wrong-copy-name': db.execute("UPDATE copies SET copy_id='other' WHERE copy_id='fast'")
        elif damage == 'missing-source-sink': db.execute('DELETE FROM cut_sinks WHERE source_part_index=1')
        elif damage == 'replica': db.execute("UPDATE copies SET is_replica=1 WHERE copy_id='fast'")
        elif damage == 'duplicate-arrival':
            db.execute("UPDATE schedule_route_edges SET sink_fpga='fpga3' WHERE route_seq=0 AND seq=1")
            db.execute("UPDATE schedule_entries SET sink_fpga='fpga3' WHERE seq=1")
        elif damage == 'cycle':
            db.execute("UPDATE schedule_route_edges SET source_fpga='fpga1' WHERE route_seq=0 AND seq=0")
            db.execute("UPDATE schedule_entries SET source_fpga='fpga1' WHERE seq=0")
    if damage == 'missing-source-sink': seal_cuts(assignment)
    with pytest.raises(EmuFlowError): produce(fixture)
    assert output.read_bytes() == before


def test_new_unrouted_crossing_is_found_from_the_source_instead_of_a_path_subset(fixture):
    produce(fixture)
    with closing(sqlite3.connect(fixture[1])) as db, db:
        db.execute("UPDATE copies SET part_index=0 WHERE copy_id='slow1'")
    before = fixture[3].read_bytes()
    with pytest.raises(EmuFlowError, match='complete original connectivity'):
        produce(fixture)
    assert fixture[3].read_bytes() == before


def test_changed_parent_generation_is_not_renewed(fixture):
    schemas = ('emuir-compact-v2', 'assignment-index-v4', 'tdm-schedule-store-v4')
    parents = [bind_store_source(p, s) for p, s in zip(fixture[:3], schemas)]
    os.utime(fixture[2], None)
    with pytest.raises(ValidationError, match='generation changed'): produce(fixture, source_bindings=parents)
    assert not fixture[3].exists()


def test_constant_high_pin_keeps_the_connected_low_bit_scalarized(fixture):
    with closing(sqlite3.connect(fixture[0])) as db, db:
        constants = json.dumps([dict(port='I0', bit=3, value='0')])
        db.execute('INSERT OR IGNORE INTO payloads(value) VALUES(?)', (constants,))
        db.execute("UPDATE cell_records SET constants_seq=(SELECT seq FROM payloads WHERE value=?) WHERE id='fast'", (constants,))
    produce(fixture)
    assert ('launch_b/Q', 'fast/I0__0', 75.) in decoded(fixture[3])


def test_pin_spelling_preserves_original_literal_backslashes_and_utf8(fixture):
    name = 'fast\\雪/child'
    with closing(sqlite3.connect(fixture[0])) as db, db:
        db.execute("UPDATE cell_records SET id=? WHERE id='fast'", (name,))
    with closing(sqlite3.connect(fixture[1])) as db, db:
        db.execute("UPDATE copies SET copy_id=? WHERE copy_id='fast'", (name,))
    produce(fixture)
    escaped = name.replace('\\', '\\\\')
    assert set(decoded(fixture[3])) == {('launch_b/Q', escaped + '/I0', 75.), (escaped + '/O', 'join/I1', 45.)}


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'), reason='explicit qualified OpenSTA required')
def test_complete_native_costs_discover_the_unselected_source_branch_in_real_sta(fixture):
    source, _, _, costs = fixture
    baseline, retimed = costs.with_suffix('.before.json'), costs.with_suffix('.retimed.json')
    call_engine(source, baseline)
    assert capture_row(baseline)['startpoint']['instance'] == 'launch_a'
    produce(fixture)
    call_engine(source, retimed, communication_delays_path=costs)
    assert capture_row(retimed)['startpoint']['instance'] == 'launch_b'
    assert capture_row(retimed)['fixed_delay_ns'] == pytest.approx(.34, abs=2e-5)
