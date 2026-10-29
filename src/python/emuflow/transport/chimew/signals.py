"""Native signal-preparation host and immutable selected-record views."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.transport.board_configuration import _record

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


class _SignalValues(Mapping):
    def __init__(self, owner, points=False):
        self.owner, self.points = owner, points

    def __len__(self):
        return self.owner.header['signals']

    def __iter__(self):
        for row in self.owner._connection().execute('SELECT entry_id FROM signals ORDER BY seq'):
            yield row[0]

    def __getitem__(self, key):
        row = self.owner._connection().execute(
            'SELECT timing_weight,source_x,source_y,sink_x,sink_y FROM signals WHERE entry_id=?',
            (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        if self.points:
            return {'source': (row[1], row[2]), 'sink': (row[3], row[4])}
        return row[0]


class ChimewSignals:
    def __init__(self, geometry, schedule, routes, output_dir, *, timing=None,
                 weight_scale=9.0, path_scope='exact-hop', region_count=4):
        self._db = None
        self._generations = {}
        try:
            output_dir = Path(output_dir).resolve()
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            sources = [_record(geometry.header_path, 'chimew-geometry-selector-v1', 'geometry'),
                       _record(schedule, 'tdm-schedule-selector-v4', 'schedule'),
                       _record(routes, 'system-route-selector-v1', 'routes')]
            if timing is not None:
                sources.append(_record(timing, 'sta-path-selector-v1', 'timing'))
            with tempfile.TemporaryDirectory(prefix='.chimew-signal-host-', dir=output_dir.parent) as temporary:
                root = Path(temporary)
                request, receipt = root / 'request.json', root / 'operation.json'
                payload = json.dumps({'schema': 'emuflow.chimew-signal-input/v1', 'sources': sources,
                    'weight_scale': weight_scale, 'path_scope': path_scope,
                    'region_count': region_count}, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(payload.encode()) > 8 * 1024 * 1024:
                    raise ValidationError('Chimew signal request exceeds bound')
                request.write_text(payload, encoding='utf-8')
                native = resolve_native_executable('emuflow-native')
                with NativeToolProcess(native, [native, '--report', str(receipt), 'phase6',
                        'prepare-chimew-signals', str(request), str(output_dir)],
                        environment=dict(os.environ), scratch_parent=root) as process:
                    result = process.finish()
                if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
                    raise ValidationError('native Chimew signal receipt is absent or exceeds bound')
                operation = json.loads(receipt.read_text())
                if result.returncode:
                    raise ValidationError('native Chimew signal preparation failed: ' +
                                          operation.get('error', {}).get('message', result.stdout))
                if (operation.get('schema') != 'emuflow.native-stage-result/v1'
                        or operation.get('stage') != 'phase6.prepare-chimew-signals'
                        or operation.get('status') != 'pass' or len(operation.get('outputs', [])) != 2):
                    raise ValidationError('native Chimew signal producer disagrees')
                for row in operation['inputs'][1:]:
                    self._bind(row)
                for row, name, schema in zip(operation['outputs'], ('signals.sqlite', 'signals.json'),
                        ('chimew-signal-store-v1', 'chimew-signal-selector-v1')):
                    if row.get('path') != str(output_dir / name) or row.get('schema') != schema:
                        raise ValidationError('native Chimew signal output owner disagrees')
                    self._bind(row)
            self.path = output_dir / 'signals.json'
            self.store_path = output_dir / 'signals.sqlite'
            if self.path.stat().st_size > 262144:
                raise ValidationError('Chimew signal selector exceeds bound')
            manifest = json.loads(self.path.read_text())
            self.header = manifest['header']
            self._db = sqlite3.connect(immutable_sqlite_uri(self.store_path), uri=True)
            if (manifest.get('schema') != 'emuflow.chimew-signal-storage/v1'
                    or manifest.get('artifact') != 'signals.sqlite'
                    or manifest.get('store_bytes') != self.store_path.stat().st_size
                    or manifest.get('store_sha256') != file_sha256(self.store_path)
                    or self._db.execute('PRAGMA application_id').fetchone()[0] != 0x454D4353
                    or self._db.execute('PRAGMA user_version').fetchone()[0] != 1
                    or self.header.get('schema') != 'emuflow.chimew-signals/v1'
                    or self.header.get('implementation_id') != 'native-chimew-signal-preparation-v1'
                    or json.loads(self._db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0]) != self.header):
                raise ValidationError('native Chimew signal store identity disagrees')
            self.weights = _SignalValues(self)
            self.points = _SignalValues(self, points=True)
            self.coverage = {key: self.header[key] for key in ('exact_path_hops', 'whole_net_fallbacks')}
            self.reference = {'path': str(self.path), 'schema': manifest['schema'],
                              'sha256': file_sha256(self.path)}
            self.require_unchanged()
        except BaseException:
            self.close()
            raise

    def _bind(self, row):
        path = Path(row['path'])
        expected = tuple(row.get('generation', {}).get(key) for key in _FIELDS)
        if (any(type(value) is not int for value in expected)
                or path in self._generations and self._generations[path] != expected):
            raise ValidationError('native Chimew signal source generation changed')
        self._generations[path] = expected

    def _connection(self):
        if self._db is None:
            raise ValidationError('Chimew signal view is closed')
        return self._db

    def crossing_entries(self):
        for row in self._connection().execute(
                'SELECT entry_id,source_slls,sink_slls,encoding FROM signals ORDER BY seq'):
            yield {'schedule_entry': row[0], 'source_slls': json.loads(row[1]),
                   'sink_slls': json.loads(row[2]), 'encoding': row[3]}

    def protected_entries(self):
        return {row[0] for row in self._connection().execute(
            'SELECT entry_id FROM signals WHERE protected=1 ORDER BY seq')}

    def require_unchanged(self):
        for path, expected in self._generations.items():
            if native_file_generation(path) != expected:
                raise ValidationError('Chimew signal input generation changed during lookahead')

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.require_unchanged()
        finally:
            self.close()
