"""Bounded native sealing of the current authoritative RouteStore rows."""
from __future__ import annotations
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing
from collections.abc import Mapping, Sequence

from .errors import ValidationError
from .sqlite_artifacts import immutable_sqlite_uri
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess

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
    store_path = Path(store_path).resolve()
    generation = native_file_generation(store_path)
    with closing(sqlite3.connect(immutable_sqlite_uri(store_path), uri=True)) as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key='sections'").fetchone()
        if row is None:
            raise ValidationError('route seal source counts are missing')
        expected = json.loads(row[0])
    with tempfile.TemporaryDirectory(prefix='.native-route-seal-', dir=store_path.parent) as temporary:
        root = Path(temporary)
        request, output = root/'request.bin', root/'seal.json'
        write_route_seal_request(request, logical)
        executable = resolve_native_executable('emuflow-native')
        command = [executable, 'phase4', 'seal-routes', str(store_path), str(request), str(output)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               scratch_parent=root, timeout_ms=21600000) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native system-route sealing failed: ' + completed.stdout)
        if native_file_generation(store_path) != generation:
            raise ValidationError('route seal source generation changed')
        if output.stat().st_size > 4096:
            raise ValidationError('native route seal exceeds summary bound')
        result = json.loads(output.read_text())
    if (not isinstance(result, dict) or result.get('schema') != 'emuflow.system-route-seal/v1'
            or result.get('implementation_id') != 'native-route-store-seal-v1'):
        raise ValidationError('native route seal summary disagrees')
    def digest(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    sections = result.get('sections')
    if not digest(result.get('logical_sha256')) or not isinstance(sections, dict) or set(sections) != set(_NAMES):
        raise ValidationError('native route seal identities are invalid')
    for name in _NAMES:
        value, original = sections[name], expected[name]
        if (not isinstance(value, dict) or type(value.get('records')) is not int
                or value['records'] != original['records'] or not digest(value.get('sha256'))
                or (original.get('sha256') is not None and value['sha256'] != original['sha256'])
                or (name == 'refinement_batch_paths' and value.get('batches') != original['batches'])):
            raise ValidationError('native route seal section disagrees: ' + name)
    return result
