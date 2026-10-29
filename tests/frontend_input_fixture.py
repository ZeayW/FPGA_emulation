"""Small source-sealed indexed inputs for native boundary lifecycle tests."""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from emuflow.common.io import write_json
from emuflow.frontend.inputs import prepare_inputs

PROFILE = 'xilinx-ultrascaleplus-open-v1'


def write_timing(root, design, clocks):
    root.mkdir(parents=True, exist_ok=True)
    for kind, schema, fmt, create, rows, summary in (
        ('paths', 'emuflow.sta-path-database/v3', 'sqlite-sta-path/v1',
         'CREATE TABLE paths(ordinal INTEGER,path_id TEXT,clock_domain TEXT,clock_period_ns REAL,slack_ns REAL,start_instance TEXT,end_instance TEXT); CREATE TABLE path_nets(path_ordinal INTEGER,net_ordinal INTEGER,net_id TEXT);',
         [('INSERT INTO paths VALUES(?,?,?,?,?,?,?)', (i, f'p{i}', clock, period, 0.0, 'a', 'b')) for i, (clock, period) in enumerate(clocks.items())], {'paths': len(clocks)}),
        ('weights', 'emuflow.partition-net-weights/v2', 'sqlite-net-weights/v1',
         'CREATE TABLE net_weights(net_id TEXT,criticality REAL,path_count INTEGER,weight REAL);',
         [('INSERT INTO net_weights VALUES(?,?,?,?)', ('n0', 1.0, 1, 1.0))], {'weighted_nets': 1}),
    ):
        store = root / (kind + '.sqlite')
        with closing(sqlite3.connect(store)) as db, db:
            db.executescript('PRAGMA user_version=1;' + create)
            for sql, values in rows:
                db.execute(sql, values)
        write_json(root / ('path-database.json' if kind == 'paths' else 'partition-net-weights.json'), {
            'schema': schema, 'design': design, 'summary': summary,
            'source': {'engine': 'opensta'}, 'payloads': {kind: {
                'path': store.name, 'format': fmt, 'records': len(rows),
                'sha256': hashlib.sha256(store.read_bytes()).hexdigest()}}})


def write_report(front, design):
    write_json(front / 'phase1_report.source.json', {'schema': 'emuflow.phase1-report/v1',
        'status': 'pass', 'design': design, 'indexed_staging_transform': {
            'primitive_audit': {'status': 'pass', 'mapping_profile': PROFILE}}})


def input_request(front, timing, output, board, clocks):
    ctx = SimpleNamespace(prepared_frontend=front, prepared_timing=timing,
        sources=(), yosys_json=None, platform_path=Path(board), output_dir=output,
        top=None, clocks=tuple(clocks), clock_periods=clocks, mapping_profile=PROFILE)
    return prepare_inputs(ctx)
