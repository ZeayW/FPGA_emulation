"""Retired assignment versions must not revive a Python query producer."""
import json
import sqlite3

import pytest

from emuflow.common.errors import ValidationError
from tests.logic_segment_query_fixture import sources, prepare
from tests.native_build import emuflow_native


@pytest.mark.parametrize('version',[1,2,3,99])
def test_query_preparation_rejects_retired_assignment_versions(tmp_path,monkeypatch,version):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    value=sources(tmp_path/'input')
    with sqlite3.connect(value.assignment) as db:
        db.execute(f'PRAGMA user_version={version}')
        db.execute('UPDATE metadata SET value=? WHERE key=?',
                   (json.dumps(f'emuflow.phase3-assignment-sqlite/v{version}'),'schema'))
    with pytest.raises(ValidationError,match='assignment v4|unsupported Phase 3 assignment'):
        prepare(value)
