"""Bounded, explicit SQL facts for exercising the native event index.

No event propagation, validation, compression, or timing reduction lives here.
"""
import json
from pathlib import Path
import sqlite3
import struct

from emuflow.common.errors import ValidationError
from emuflow.timing.global_event_store import _Owner, EventChecks, PathMetadata

_ROLES = ('target', 'runtime', 'tx', 'commit')


class EventFacts:
    def __init__(self, *, bound=False, metadata_only=False):
        self._owner = _Owner()
        self._owner.mode = 2 if metadata_only else 3 if bound else 1
        db = self._owner.connection = sqlite3.connect(self._owner.primary)
        db.executescript('PRAGMA application_id=1162692421; PRAGMA user_version=1;'
            + (Path(__file__).parents[1] / 'src/python/emuflow/data/schema/global_events.sql').read_text())
        self._path_ids, self._event_ids = {}, {}
        self._finished = False

    def append_values(self, path, role, event, launch, arcs, required):
        from emuflow.timing.global_sta import EventCheck
        self.append(EventCheck(path, role, event, launch, arcs, required))

    def append(self, row):
        if self._finished:
            raise ValidationError('event facts already sealed')
        owner, db = self._owner, self._owner.connection
        assert owner.checks < 100000, 'fixture exceeds explicit fact bound'
        try:
            if row.path not in self._path_ids:
                seq = len(self._path_ids)
                self._path_ids[row.path] = seq
                db.execute('INSERT INTO paths(seq,path) VALUES(?,?)', (seq, row.path))
            if row.event not in self._event_ids:
                seq = len(self._event_ids)
                self._event_ids[row.event] = seq
                db.execute('INSERT INTO events VALUES(?,?)', (seq, row.event))
            db.execute('INSERT INTO checks VALUES(?,?,?,?,?)', (owner.checks,
                self._path_ids[row.path], _ROLES.index(row.role), self._event_ids[row.event],
                struct.pack('<'+'d'*(2+len(row.arcs_ns)), row.launch_ns, row.required_ns, *row.arcs_ns)))
            owner.checks += 1
        except (sqlite3.IntegrityError, ValueError, TypeError) as error:
            raise ValidationError('invalid explicit event facts: '+str(error)) from error

    def add_metadata(self, path, **values):
        if self._finished:
            raise ValidationError('event facts already sealed')
        assert len(self._path_ids) < 50000
        seq = len(self._path_ids)
        self._path_ids[path] = seq
        try:
            self._owner.connection.execute('INSERT INTO paths VALUES(?,?,?,?,?,?,?,?)', (
                seq, path, values['representative_path'],
                int(values['path_scope']=='same-fpga-local'), values['clock_domain'],
                values['target_period_ns'], values['physical_logic_segments_cone_bound'],
                values.get('interconnect_delay_model')))
        except sqlite3.IntegrityError as error:
            raise ValidationError('duplicate/overlap metadata facts') from error

    def finish(self):
        if self._finished:
            raise ValidationError('event facts already sealed')
        owner = self._owner
        owner.paths, owner.events = len(self._path_ids), len(self._event_ids)
        header = dict(schema='emuflow.global-event-checks/v1', mode=owner.mode,
            paths=owner.paths, checks=owner.checks, events=owner.events)
        owner.connection.execute("INSERT INTO metadata VALUES('header',?)", (json.dumps(header),))
        owner.connection.commit(); owner.connection.close(); owner.connection = None
        self._finished = owner.finished = True
        try:
            owner._open_index(header)
        except BaseException:
            owner.close()
            raise
        return PathMetadata(owner) if owner.mode==2 else EventChecks(owner)


class MetadataFacts:
    def __init__(self, events=None):
        self.facts = events if events is not None else EventFacts(metadata_only=True)
        self._owner = self.facts._owner
        self._finished = False

    def append(self, path, **values):
        self.facts.add_metadata(path, **values)

    def finish(self):
        if self._finished:
            raise ValidationError("metadata facts already sealed")
        self._finished = True
        if not self.facts._finished:
            self.facts.finish()
        return PathMetadata(self._owner)
