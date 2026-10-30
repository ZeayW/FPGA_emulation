"""Bounded host and selected-point reader for the native Chimew geometry owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.platform.platform import Platform
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow_tools.transport.board_configuration import _record, _wiring_file
from emuflow_tools.transport.chimew.chimew_fixed_wiring import FixedWiringChannels
from emuflow_tools.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
from emuflow_tools.transport.virtual_board import INVENTORY_PATH, FIXED_WIRING_ROOT

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


class XilinxChimewGeometry:
    """Read an immutable native projection of the schedule's selected endpoints.

    Source/mapped/placement joins, forwarded-register coverage, native tile
    coordinates and source seals all belong to the C++ producer. This host
    carries board-sized facts and queries only the published point relation.
    """

    def __init__(self, source: Path, platform_path: Path, report, architecture: Path,
                 *, schedule_path: Path, output_dir: Path | None = None,
                 package_geometry=None):
        self._db = None
        self._temporary = None
        self._generations = {}
        try:
            platform_path = Path(platform_path).resolve()
            architecture = Path(architecture).resolve()
            platform = Platform.load(platform_path)
            parts = {fpga.part for fpga in platform.fpgas}
            if len(parts) != 1:
                raise ValidationError('Chimew lookahead requires one shared device geometry')
            catalog = package_geometry or XilinxPackageGeometry.open(architecture, next(iter(parts)))
            if (type(catalog) is not XilinxPackageGeometry or catalog.path != architecture
                    or catalog.part != next(iter(parts))):
                raise ValidationError('Chimew package geometry belongs to a different device artifact')
            snapshot = catalog.control_snapshot()
            finite_catalog = {**snapshot, 'path': str(snapshot['path']),
                              'generation': list(snapshot['generation']),
                              'pins': {name: list(point) for name, point in snapshot['pins'].items()}}
            board = platform.to_dict()
            rows = [_record(platform_path, 'boarddb-v1', 'board'),
                    _record(source, 'emuir-compact-v2', 'original'),
                    _record(architecture, 'physical-input-document-v1', 'architecture'),
                    _record(schedule_path, 'tdm-schedule-selector-v4', 'schedule'),
                    _record(INVENTORY_PATH, 'hp-package-pin-inventory-v1', 'inventory')]
            wiring = _wiring_file(board, FIXED_WIRING_ROOT)
            if wiring is not None:
                rows.append(_record(wiring, 'fixed-wiring-csv-v1', 'wiring'))
            if output_dir is None:
                self._temporary = tempfile.TemporaryDirectory(prefix='chimew-geometry-')
                output_dir = Path(self._temporary.name) / 'projection'
            output_dir = Path(output_dir).resolve()
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.chimew-geometry-host-', dir=output_dir.parent) as temporary:
                root = Path(temporary)
                request, proof = root / 'request.json', root / 'operation.json'
                text = json.dumps({'schema': 'emuflow.chimew-geometry-input/v1', 'sources': rows,
                                   'package_geometry': finite_catalog, 'physical_report': report},
                                  sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(text.encode()) > 8 * 1024 * 1024:
                    raise ValidationError('Chimew geometry request exceeds bound')
                request.write_text(text, encoding='utf-8')
                native = resolve_native_executable('emuflow-native')
                with NativeToolProcess(native, [native, '--report', str(proof), 'phase6',
                        'prepare-chimew-geometry', str(request), str(output_dir)],
                        environment=dict(os.environ), scratch_parent=root) as process:
                    result = process.finish()
                if result.returncode:
                    message = result.stdout
                    if proof.is_file() and proof.stat().st_size <= 1024 * 1024:
                        message = json.loads(proof.read_text())['error']['message']
                    raise ValidationError('native Chimew geometry failed: ' + message)
                if proof.stat().st_size > 1024 * 1024:
                    raise ValidationError('native Chimew geometry receipt exceeds bound')
                operation = json.loads(proof.read_text())
                if (operation.get('schema') != 'emuflow.native-stage-result/v1'
                        or operation.get('stage') != 'phase6.prepare-chimew-geometry'
                        or operation.get('status') != 'pass' or len(operation.get('outputs', [])) != 2):
                    raise ValidationError('native Chimew geometry producer disagrees')
                for row in operation['inputs'][1:]:
                    self._bind_receipt(row)
                for row, name, schema in zip(operation['outputs'],
                        ('geometry.sqlite', 'geometry.json'),
                        ('chimew-geometry-store-v1', 'chimew-geometry-selector-v1')):
                    if row.get('path') != str(output_dir / name) or row.get('schema') != schema:
                        raise ValidationError('native Chimew geometry output owner disagrees')
                    self._bind_receipt(row)
            self.store_path, self.header_path = output_dir / 'geometry.sqlite', output_dir / 'geometry.json'
            if self.header_path.stat().st_size > 8 * 1024 * 1024:
                raise ValidationError('native Chimew geometry header exceeds bound')
            self.manifest = json.loads(self.header_path.read_text())
            header = self.manifest['header']
            self._db = sqlite3.connect(immutable_sqlite_uri(self.store_path), uri=True)
            if (self.manifest.get('schema') != 'emuflow.chimew-geometry-storage/v1'
                    or self.manifest.get('artifact') != 'geometry.sqlite'
                    or self.manifest.get('store_bytes') != self.store_path.stat().st_size
                    or type(self.manifest.get('store_sha256')) is not str
                    or len(self.manifest['store_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in self.manifest['store_sha256'])
                    or self._db.execute('PRAGMA application_id').fetchone()[0] != 0x454D4347
                    or self._db.execute('PRAGMA user_version').fetchone()[0] != 1
                    or header.get('schema') != 'emuflow.chimew-geometry/v1'
                    or header.get('implementation_id') != 'native-indexed-xilinx-geometry-v1'
                    or json.loads(self._db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0]) != header):
                raise ValidationError('native Chimew geometry store identity disagrees')
            self.header = header
            self.bounds = {fpga: tuple(box) for fpga, box in header['bounds'].items()}
            self.pins = {(fpga, pin): (x, y) for fpga, pin, x, y in header['pins']}
            self.sources, self.architecture = header['sources'], header['architecture']
            self.reference = {'path': str(self.header_path),
                              'schema': 'emuflow.chimew-geometry-storage/v1',
                              'sha256': file_sha256(self.header_path)}
            self.forwarded = header['metrics']['forwarded']
            self.fixed = FixedWiringChannels(platform)
            self.require_unchanged()
        except BaseException:
            self.close()
            raise

    def _bind_receipt(self, row):
        path = Path(row['path'])
        expected = tuple(row.get('generation', {}).get(key) for key in _FIELDS)
        if len(expected) != 7 or any(type(value) is not int for value in expected):
            raise ValidationError('invalid native Chimew geometry source receipt')
        if path in self._generations and self._generations[path] != expected:
            raise ValidationError('native Chimew geometry source generation changed')
        self._generations[path] = expected

    def require_unchanged(self):
        for path, expected in self._generations.items():
            if native_file_generation(path) != expected:
                raise ValidationError('Chimew physical input generation changed during lookahead')

    def point(self, fpga, net, entry, kind):
        if self._db is None:
            raise ValidationError('Chimew geometry view is closed')
        row = self._db.execute('SELECT x,y,normal_y FROM points '
                              'WHERE entry_id=? AND kind=? AND fpga=? AND net=?',
                              (entry, kind, fpga, net)).fetchone()
        if row is None:
            raise ValidationError('Chimew query is absent from the selected schedule endpoint owner')
        return tuple(float(value) for value in row)

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.require_unchanged()
        finally:
            self.close()
