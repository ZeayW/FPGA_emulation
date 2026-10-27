"""Canonical representative OpenSTA scalars with original-identity cursor views."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.timing.global_event_store import EventChecks, _native
from emuflow.common.io import file_sha256, write_json
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.storage.store_publication import bind_store_source, publish_sqlite_store, require_store_sources_unchanged

_IMPL = 'native-global-measurements-v1'
_SCHEMA = 'emuflow.global-measurement-storage/v1'
_ROLES = ('target', 'runtime', 'tx', 'commit')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _operation(rows, command, sources, output, *, magic, flags=(), text=()):
    rows.require_valid()
    with tempfile.TemporaryDirectory(prefix='.global-numerical-', dir=rows._owner.root) as temporary:
        request = Path(temporary) / 'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', magic, 1)
            for flag in flags:
                writer.put('I', flag)
            for source in sources:
                writer.put('3Q4q', *source.generation)
            for value in text:
                writer.text(value)
            writer.finish()
            _native([command, *(source.path for source in sources), request, output], rows._owner.root)
        finally:
            writer.close()
    rows.require_valid()
    require_store_sources_unchanged(sources)


def _header(path):
    with closing(sqlite3.connect(immutable_sqlite_uri(path), uri=True)) as connection:
        raw = connection.execute("SELECT value FROM metadata WHERE key='header'").fetchone()
    if raw is None or not isinstance(raw[0], str) or len(raw[0]) > 65536:
        raise ValidationError('native measurement header is missing/oversized')
    value = json.loads(raw[0])
    if (not isinstance(value, dict) or value.get('schema') != 'emuflow.global-measurements/v1'
            or value.get('implementation_id') != _IMPL):
        raise ValidationError('native measurement schema/implementation disagrees')
    return value


@contextmanager
def _staged_file(directory):
    descriptor, name = tempfile.mkstemp(prefix='.measurement-', suffix='.sqlite', dir=directory)
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def import_measurements(path, rows, *, verify_arcs=True, directory=None):
    """Import engine output once; retain S representative rows rather than H copies."""
    if not isinstance(rows, EventChecks):
        raise ValidationError('native measurement import requires event ownership')
    owner = rows._owner
    destination = Path(directory) if directory is not None else owner.root
    destination.mkdir(parents=True, exist_ok=True)
    sources = (*owner.sources, bind_store_source(path, 'global-measurement-tsv-v1'))
    with _staged_file(destination) as temporary:
        _operation(rows, 'import-measurements', sources, temporary, magic=0x314D4947464D5545,
                   flags=(int(rows.representatives), int(verify_arcs)))
        value = _header(temporary)
        digest = file_sha256(temporary)
        store = destination / f'measurements.{digest}.sqlite'
        if store.is_symlink():
            raise ValidationError('measurement generation is a symbolic link')
        if store.exists():
            if file_sha256(store) != digest:
                raise ValidationError('measurement generation seal disagrees')
        else:
            publish_sqlite_store(temporary, store, 'global-measurements-store-v1', sources=sources)
        source = bind_store_source(store, 'global-measurements-store-v1')
        manifest = None
        if directory is not None:
            manifest = destination / 'measurements.json'
            require_store_sources_unchanged((*sources, source))
            write_json(manifest, {
                'schema': _SCHEMA, 'artifact': store.name, 'store_bytes': source.generation[2],
                'store_sha256': digest, 'source_logical_sha256': value['source_logical_sha256'],
                'measurement_logical_sha256': value['reduction']['measurements_sha256'],
            }, compact=True)
    return NativeMeasurements(rows, source, value, manifest=manifest)


def read_native_measurements(path, rows, *, verify_arcs=True):
    """Explicit saved-result validation; reconstruct facts, never rerun OpenSTA."""
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 4096:
        raise ValidationError('native measurement selector is invalid')
    selector_source = bind_store_source(path, 'global-measurement-selector-v1')
    value = json.loads(path.read_text())
    fields = {'schema', 'artifact', 'store_bytes', 'store_sha256', 'source_logical_sha256', 'measurement_logical_sha256'}
    if not isinstance(value, dict) or set(value) != fields or value['schema'] != _SCHEMA:
        raise ValidationError('native measurement selector schema is invalid')
    digest = value['store_sha256']
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)
            or value['artifact'] != f'measurements.{digest}.sqlite'):
        raise ValidationError('native measurement generation identity is invalid')
    store = path.parent / value['artifact']
    source = bind_store_source(store, 'global-measurements-store-v1')
    if (store.is_symlink() or type(value['store_bytes']) is not int
            or source.generation[2] != value['store_bytes'] or file_sha256(store) != digest):
        raise ValidationError('native measurement store seal disagrees')
    header = _header(store)
    if (value['source_logical_sha256'] != header['source_logical_sha256']
            or value['measurement_logical_sha256'] != header['reduction']['measurements_sha256']):
        raise ValidationError('native measurement logical selector disagrees')
    result = NativeMeasurements(rows, source, header, manifest=path)
    try:
        with tempfile.TemporaryDirectory(prefix='.measurement-audit-', dir=rows._owner.root) as temporary:
            summary = Path(temporary) / 'summary.json'
            _operation(rows, 'audit-measurements', result.sources[:3], summary,
                       magic=0x31415647464D5545, flags=(int(verify_arcs),))
            if summary.stat().st_size > 65536 or json.loads(summary.read_text()) != header['reduction']:
                raise ValidationError('native measurement audit summary disagrees')
        require_store_sources_unchanged((selector_source, *result.sources))
        return result
    except BaseException:
        result.close()
        raise


class NativeMeasurements(Sequence):
    def __init__(self, rows, source, header, *, manifest=None):
        owner = rows._owner
        owner.require()
        if (header['source_logical_sha256'] != owner.header['logical_sha256']
                or header['checks'] != owner.checks
                or header['representative_checks'] != owner.header['representative_checks']):
            raise ValidationError('native measurement event namespace disagrees')
        self.rows = EventChecks(owner)
        self.header, self.store_path = header, source.path
        self.sources = (*owner.sources, source)
        if manifest is not None:
            self.sources += (bind_store_source(manifest, 'global-measurement-selector-v1'),)
        self.closed = False
        self._borrows = 0
        self.connection = sqlite3.connect(immutable_sqlite_uri(source.path), uri=True)
        owner._dependents.append(self.connection)
        try:
            for alias, path in [('facts', owner.primary), ('idx', owner.index)]:
                self.connection.execute(f'ATTACH DATABASE ? AS {alias}', (immutable_sqlite_uri(path),))
            self.connection.executescript('PRAGMA cache_size=-8192; PRAGMA facts.cache_size=-8192; PRAGMA idx.cache_size=-8192; PRAGMA temp_store=FILE;')
            self._borrows = 0
            self.require()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.closed = True
        if not self._borrows:
            self._dispose()

    def _dispose(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()
            dependents = self.rows._owner._dependents
            if connection in dependents:
                dependents.remove(connection)

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass

    def require(self):
        self.rows._owner.require()
        if self.closed or self.connection is None:
            raise ValidationError('native measurement owner is closed')
        if not self._borrows:
            require_store_sources_unchanged(self.sources)

    @contextmanager
    def borrow(self):
        self.require()
        with self.rows._owner.borrow():
            self._borrows += 1
            try:
                yield
            finally:
                self._borrows -= 1
                if self.closed and not self._borrows:
                    self._dispose()
        self.require()

    def __len__(self):
        return self.header['checks']

    def _record(self, row):
        self.require()
        if row is None:
            raise ValidationError('missing native measurement record')
        launch = struct.unpack_from('<d', row[3])[0]
        arrival, required, slack = struct.unpack('<3d', row[4])
        return {'path': row[0], 'role': _ROLES[row[1]], 'event': row[2],
                'arrival_ns': arrival+launch, 'required_ns': required+launch, 'slack_ns': slack}

    def __getitem__(self, index):
        self.require()
        if isinstance(index, slice):
            return [self[n] for n in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        cursor = self.connection.execute(_MEASUREMENTS_SQL+' WHERE c.seq=?', (index,))
        try:
            return self._record(cursor.fetchone())
        finally:
            cursor.close()

    def __iter__(self):
        with self.borrow():
            cursor = self.connection.execute(_MEASUREMENTS_SQL+' ORDER BY c.seq')
            try:
                for row in cursor:
                    yield self._record(row)
            finally:
                cursor.close()

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a,b in zip(self,other))


_MEASUREMENTS_SQL = '''SELECT p.path,c.role,e.event,c.numbers,m.numbers FROM facts.checks c
JOIN facts.paths p ON p.seq=c.path_seq JOIN facts.events e ON e.seq=c.event_seq
JOIN idx.path_index i ON i.path_seq=c.path_seq
JOIN measurements m ON m.seq=i.engine_first_check+c.seq-i.first_check'''


class NativeTimingPaths(Sequence):
    def __init__(self, measurements, metadata):
        if metadata._owner is not measurements.rows._owner or metadata._owner.mode != 3:
            raise ValidationError('native timing requires matching bound path metadata')
        self.measurements, self.metadata = measurements, metadata

    def __len__(self):
        return self.measurements.header['reduction']['paths']

    def _record(self, row):
        self.measurements.require()
        if row is None:
            raise ValidationError('native timing path is missing')
        target, runtime = struct.unpack('<3d', row[9]), struct.unpack('<3d', row[10])
        target_launch, runtime_launch = struct.unpack_from('<d',row[7])[0], struct.unpack_from('<d',row[8])[0]
        values = [target[0]+target_launch,target[1]+target_launch,target[2],runtime[1]+runtime_launch,runtime[2]]
        return {'path': row[0], 'representative_path': row[1], 'path_scope': 'same-fpga-local' if row[2] else 'cross-fpga',
                'clock_domain': row[3], 'target_period_ns': row[4], 'physical_logic_segments_cone_bound': bool(row[5]),
                **({'interconnect_delay_model': row[6]} if row[6] is not None else {}),
                **dict(zip(('system_delay_bound_ns','target_required_time_ns','target_clock_slack_bound_ns',
                            'runtime_required_time_ns','runtime_clock_slack_bound_ns'),(0.0 if n==0 else n for n in values)))}

    def __getitem__(self, index):
        if isinstance(index,slice):
            return [self[n] for n in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        self.measurements.require()
        cursor=self.measurements.connection.execute(_PATHS_SQL+' WHERE o.position=?',(index,))
        try:
            return self._record(cursor.fetchone())
        finally:
            cursor.close()

    def __iter__(self):
        with self.measurements.borrow():
            cursor=self.measurements.connection.execute(_PATHS_SQL+' ORDER BY o.position')
            try:
                for row in cursor:
                    yield self._record(row)
            finally:
                cursor.close()

    def __eq__(self,other):
        return isinstance(other,Sequence) and len(self)==len(other) and all(a==b for a,b in zip(self,other))

    def bound_store_sources(self):
        self.measurements.require()
        return self.measurements.sources

    def publish_store(self,path,header):
        self.measurements.require()
        _operation(self.measurements.rows,'publish-qor',self.measurements.sources[:3],path,
                   magic=0x31515047464D5545,text=(_canonical(header),))
        with closing(sqlite3.connect(immutable_sqlite_uri(path),uri=True)) as connection:
            values={key:json.loads(raw) for key,raw in connection.execute('SELECT key,value FROM metadata')}
        if values.get('header')!=header or values.get('counts')!={'timing_paths':len(self)}:
            raise ValidationError('native QoR header/counts disagree')
        return values['counts'],values['logical_sha256']


_PATHS_SQL='''SELECT p.path,p.representative_path,p.path_scope,p.clock_domain,p.target_period_ns,p.cone_bound,
p.interconnect_delay_model,t.numbers,r.numbers,mt.numbers,mr.numbers
FROM idx.path_order o JOIN facts.paths p ON p.seq=o.path_seq JOIN idx.path_index i ON i.path_seq=p.seq
JOIN facts.checks t ON t.path_seq=p.seq AND t.role=0 JOIN facts.checks r ON r.path_seq=p.seq AND r.role=1
JOIN measurements mt ON mt.seq=i.engine_first_check+t.seq-i.first_check
JOIN measurements mr ON mr.seq=i.engine_first_check+r.seq-i.first_check'''


def validate_native_qor(path,header,logical_sha):
    source=bind_store_source(path,'qor-report-store-v1')
    with tempfile.TemporaryDirectory(prefix='emuflow-qor-validation-') as temporary:
        root=Path(temporary);request=root/'request.bin';summary=root/'summary.json'
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QI',0x31565147464D5545,1);writer.put('3Q4q',*source.generation)
            writer.text(_canonical(header));writer.text(logical_sha);writer.finish()
            _native(['validate-qor',source.path,request,summary],root)
        finally:
            writer.close()
        value=json.loads(summary.read_text())
        if (value.get('status')!='pass' or value.get('logical_sha256')!=logical_sha
                or value.get('paths')!=header['timing']['summary']['timing_paths']):
            raise ValidationError('native QoR validation result disagrees')
        require_store_sources_unchanged((source,))
