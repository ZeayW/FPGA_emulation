"""Shape-only protocol fixture for the retained native RouteStore seal kernel."""
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import subprocess
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from tests.native_build import emuflow_native

_RELATIONS = {
    ('demands',): 0,
    ('routes',): 1,
    ('timing', 'paths'): 2,
    ('joint_optimization', 'candidate_generation', 'master_selection'): 3,
    ('joint_optimization', 'refinement_batches', 'batches'): 4,
}
_NAMES = ('demands', 'routes', 'timing_paths', 'master_selections', 'refinement_batch_paths')
_MAX_REQUEST = 8 * 1024 * 1024


def write_route_seal_request(path, logical):
    """Encode bounded header literals and relation references, never their rows."""
    writer = CheckedRequestWriter(path)
    nodes = 0
    def visit(value, location=()):
        nonlocal nodes
        nodes += 1
        if nodes > 65536 or len(location) > 64:
            raise ValidationError('route seal header tree exceeds bound')
        if location in _RELATIONS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValidationError('route seal relation is not an array')
            writer.put('BBQ', 2, _RELATIONS[location], len(value))
        elif any(key[:len(location)] == location for key in _RELATIONS) and isinstance(value, Mapping):
            if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
                raise ValidationError('route seal header object is invalid')
            writer.put('BI', 1, len(value))
            for key in sorted(value):
                writer.text(key)
                visit(value[key], (*location, key))
        else:
            writer.put('B', 0)
            literal = io.StringIO()
            encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            for chunk in encoder.iterencode(value):
                if literal.tell() + len(chunk) > _MAX_REQUEST:
                    raise ValidationError('route seal header literal exceeds bound')
                literal.write(chunk)
            writer.text(literal.getvalue())
        if writer.stream.tell() > _MAX_REQUEST - 4:
            raise ValidationError('route seal request exceeds bound')
    try:
        writer.put('QI', 0x31515352464D5545, 1)
        visit(logical)
        writer.finish()
    finally:
        writer.close()



def seal_native_system_routes(store_path, logical):
    with tempfile.TemporaryDirectory(prefix='route-seal-fixture-') as temporary:
        request, output = Path(temporary)/'request.bin', Path(temporary)/'seal.json'
        write_route_seal_request(request, logical)
        result = subprocess.run([str(emuflow_native()), 'phase4', 'seal-routes',
            str(store_path), str(request), str(output)], capture_output=True, text=True)
        if result.returncode:
            raise ValidationError('native route seal kernel failed: '+result.stdout+result.stderr)
        return json.loads(output.read_text())


def write_route_store_fixture(selector, logical):
    """Encode current v3 test facts, without a production publisher lifecycle.

    Hash expectations are computed independently in Python. Native sealing and
    the full native stage are exercised by their own tests, not by this encoder.
    """
    import hashlib
    import sqlite3

    canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    routes, demands = list(logical['routes']), list(logical['demands'])
    header = {key: value for key, value in logical.items() if key not in {'routes', 'demands'}}
    timing = dict(header.get('timing', {}))
    paths = list(timing.pop('paths', []))
    if 'timing' in header:
        header['timing'] = timing
    joint = dict(header.get('joint_optimization', {}))
    candidate = dict(joint.get('candidate_generation', {}))
    selections = list(candidate.pop('master_selection', []))
    refinement = dict(joint.get('refinement_batches', {}))
    batches = list(refinement.pop('batches', []))
    if 'candidate_generation' in joint:
        joint['candidate_generation'] = candidate
    if 'refinement_batches' in joint:
        joint['refinement_batches'] = refinement
    if 'joint_optimization' in header:
        header['joint_optimization'] = joint
    batch_rows = [dict(batch=batch, position=position, path=path)
                  for batch, members in enumerate(batches) for position, path in enumerate(members)]
    relations = (demands, routes, paths, selections, batch_rows)
    if sum(map(len, relations)) > 65536:
        raise AssertionError('RouteStore SQL encoder is restricted to test fixtures')
    sections = {name: dict(records=len(records), sha256=hashlib.sha256(
                    ''.join(canonical(row) + '\n' for row in records).encode()).hexdigest())
                for name, records in zip(_NAMES, relations)}
    sections['refinement_batch_paths']['batches'] = len(batches)
    logical_hash = hashlib.sha256((json.dumps(logical, sort_keys=True, indent=2) + '\n').encode()).hexdigest()
    selector = Path(selector)
    selector.parent.mkdir(parents=True, exist_ok=True)
    # Scratch files and repeat writes are fixture concerns only: no source
    # recapture, previous-generation deletion, rollback, or compatibility path.
    with tempfile.TemporaryDirectory(prefix='route-facts-', dir=selector.parent) as temporary:
        store = Path(temporary)/'facts.sqlite'
        with sqlite3.connect(store) as db:
            ddl = Path(__file__).parents[1]/'src/python/emuflow/data/schema/phase4_routes.sql'
            db.executescript(ddl.read_text())
            db.execute('PRAGMA application_id=1162696018')
            db.execute('PRAGMA user_version=3')
            fields = ('width_bits', 'transport_round', 'cut_class', 'dependency_level',
                      'combinational_dependency_depth', 'max_latency_cycles', 'predicted_max_delay_ns')
            for seq, row in enumerate(routes):
                db.execute('INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (seq, row['id'], row['net'], row['source'], *(row.get(key) for key in fields)))
                db.executemany('INSERT INTO route_sinks VALUES(?,?,?)',
                               ((seq, index, sink) for index, sink in enumerate(row['sinks'])))
                db.executemany('INSERT INTO route_edges VALUES(?,?,?,?,?)',
                               ((seq, index, edge['link'], edge['from'], edge['to'])
                                for index, edge in enumerate(row['tree_edges'])))
                db.executemany('INSERT INTO route_predecessors VALUES(?,?,?)',
                               ((seq, index, net) for index, net in enumerate(row.get('predecessor_cut_nets', []))))
            db.executemany('INSERT INTO demands VALUES(?,?,?)',
                           ((seq, row['id'], seq) for seq, row in enumerate(demands)))
            db.executemany('INSERT INTO timing_paths VALUES(?,?,?)',
                           ((seq, row.get('path', row.get('id')), canonical(row)) for seq, row in enumerate(paths)))
            db.executemany('INSERT INTO route_master_selections VALUES(?,?,?)',
                           ((seq, row['demand'], row['generator']) for seq, row in enumerate(selections)))
            db.executemany('INSERT INTO refinement_batch_paths VALUES(?,?,?,?)',
                           ((seq, row['batch'], row['position'], row['path']) for seq, row in enumerate(batch_rows)))
            db.executemany('INSERT INTO metadata VALUES(?,?)',
                           ((key, canonical(value)) for key, value in
                            (('header', header), ('sections', sections), ('logical_sha256', logical_hash))))
        digest = hashlib.sha256(store.read_bytes()).hexdigest()
        destination = selector.parent/f'routes.{digest}.sqlite'
        if not destination.exists():
            destination.write_bytes(store.read_bytes())
    manifest = dict(schema='emuflow.system-route-storage/v1', logical_schema='emuflow.system-routes/v1',
                    application_id=0x454D5552, user_version=3, header=header, sections=sections,
                    logical_sha256=logical_hash, artifact=destination.name,
                    store_bytes=destination.stat().st_size, store_sha256=digest)
    selector.write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    return manifest
