"""Small source-sealed indexed inputs for native boundary lifecycle tests."""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from emuflow.common.io import write_json
from emuflow.frontend.inputs import prepare_inputs

PROFILE = 'xilinx-ultrascaleplus-open-v1'


def write_timing(root, design, clocks, source_ir):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_ir)) as db:
        net = db.execute('SELECT id FROM net_records ORDER BY seq LIMIT 1').fetchone()[0]
    path_schema = (Path(__file__).resolve().parents[1] /
        'src/python/emuflow/data/schema/sta_paths.sql').read_text()
    path_rows = []
    for i, (clock, period) in enumerate(clocks.items()):
        path_rows.extend([
            ('INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (i, f'p{i}', clock, period, 0.0, 1.0, *([None] * 8))),
            ('INSERT INTO path_nets VALUES(?,?,?)', (i, 0, net)),
        ])
    for kind, schema, fmt, create, rows, summary, records in (
        ('paths', 'emuflow.sta-path-database/v3', 'sqlite-sta-path/v1',
         path_schema, path_rows,
         {'paths': len(clocks), 'structured_endpoint_paths': 0, 'unique_path_nets': 1,
          'qor': {'wns_ns': 0.0, 'tns_ns': 0.0, 'failing_endpoints': 0,
                  'timed_endpoints': len(clocks)}}, len(clocks)),
        ('weights', 'emuflow.partition-net-weights/v2', 'sqlite-net-weights/v1',
         'CREATE TABLE net_weights(net_id TEXT,criticality REAL,path_count INTEGER,weight REAL);',
         [('INSERT INTO net_weights VALUES(?,?,?,?)', (net, 1.0, 1, 1.0))], {'weighted_nets': 1}, 1),
    ):
        store = root / (kind + '.sqlite')
        with closing(sqlite3.connect(store)) as db, db:
            db.executescript('PRAGMA user_version=1;' + create)
            for sql, values in rows:
                db.execute(sql, values)
        value = {
            'schema': schema, 'design': design, 'summary': summary,
            'source': {'engine': 'opensta', 'provider': 'opensta-fpga-path-database-v1'},
            'payloads': {kind: {'path': store.name, 'format': fmt, 'records': records,
                'sha256': hashlib.sha256(store.read_bytes()).hexdigest()}}}
        if kind == 'paths':
            value['normalization'] = {'positive_slack_scale_ns': 1.0,
                'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': max(clocks.values())}
        write_json(root / ('path-database.json' if kind == 'paths' else 'partition-net-weights.json'), value)



def write_report(front, design):
    write_json(front / 'phase1_report.source.json', {'schema': 'emuflow.phase1-report/v1',
        'status': 'pass', 'design': design, 'indexed_staging_transform': {
            'primitive_audit': {'status': 'pass', 'mapping_profile': PROFILE}}})


def input_request(front, timing, output, board, clocks):
    ctx = SimpleNamespace(prepared_frontend=front, prepared_timing=timing,
        sources=(), yosys_json=None, platform_path=Path(board), output_dir=output,
        top=None, clocks=tuple(clocks), clock_periods=clocks, mapping_profile=PROFILE)
    return prepare_inputs(ctx)
