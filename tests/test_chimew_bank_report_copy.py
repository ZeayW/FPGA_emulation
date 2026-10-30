"""Report transfer stays in the native typed owner, including validation."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.transport.chimew.chimew_bank_channel import evaluate_chimew_bank_channel_assignment as assign
from emuflow_tools.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage
from emuflow_tools.transport.chimew import chimew_bank_channel_report_storage as storage
from tests.native_build import emuflow_native
from tests.test_chimew_bank_native_store import document


def create(root, name, design=None):
    raw = document()
    if design is not None:
        raw['design'] = design
    with write_chimew_bank_channel_storage(root / (name + '-input.json'), raw) as source:
        return assign(source, output_path=root / (name + '.json'))


@pytest.mark.parametrize('name', ['copied.json', 'copied.' + 'a' * 64 + '.json'])
def test_copy_preserves_records_payload_bytes_and_logical_seals_without_python_rows(tmp_path, name):
    with create(tmp_path, 'source') as source:
        expected = source.materialize()
        source_bytes = source.store_path.read_bytes()
        output = tmp_path / 'output' / name
        with patch.object(storage._AssignmentSequence, '__iter__', side_effect=AssertionError('Python assignment scan')):
            storage.copy_chimew_bank_channel_report_storage(source.manifest_path, output)
        with storage.read_chimew_bank_channel_report(output) as copied:
            assert copied.materialize() == expected
            assert copied.store_path.read_bytes() == source_bytes
            assert copied.logical_sha256 == source.logical_sha256
            assert copied.manifest['store_sha256'] == source.manifest['store_sha256']


def test_copy_to_same_owner_checks_source_without_replacing_it(tmp_path):
    with create(tmp_path, 'source') as source:
        files = [source.manifest_path, source.store_path]
        before = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in files]
        storage.copy_chimew_bank_channel_report_storage(source.manifest_path, source.manifest_path)
        assert [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in files] == before


def test_copy_waits_for_previous_reader_and_reclaims_only_old_owned_payload(tmp_path):
    old = create(tmp_path, 'destination', 'previous')
    old_store = old.store_path
    with create(tmp_path, 'source', 'replacement') as source:
        started, done = threading.Event(), threading.Event()
        errors = []
        def replace():
            started.set()
            try:
                storage.copy_chimew_bank_channel_report_storage(source.manifest_path, old.manifest_path)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=replace)
        worker.start()
        assert started.wait(5)
        assert not done.wait(0.15)
        assert old['design'] == 'previous' and old_store.exists()
        old.close()
        assert done.wait(15)
        worker.join()
        assert not errors and not old_store.exists()
        with storage.read_chimew_bank_channel_report(tmp_path / 'destination.json') as copied:
            assert copied['design'] == 'replacement'
        assert source.store_path.is_file()


@pytest.mark.parametrize('failure', ['version', 'application', 'schema', 'extra-metadata', 'negative-cost', 'relation', 'logical'])
def test_invalid_report_never_replaces_a_published_destination(tmp_path, failure):
    with create(tmp_path, 'destination') as old:
        output, old_store = old.manifest_path, old.store_path
    before, old_payload = output.read_bytes(), old_store.read_bytes()
    with create(tmp_path, 'source') as source:
        path, payload = source.manifest_path, source.store_path
    manifest = json.loads(path.read_text())
    if failure in ('relation', 'logical'):
        if failure == 'relation':
            manifest['assignments']['sha256'] = '0' * 64
        else:
            manifest['logical_sha256'] = '0' * 64
    else:
        with closing(sqlite3.connect(payload)) as db, db:
            if failure == 'version':
                db.execute('PRAGMA user_version=2')
            elif failure == 'application':
                db.execute('PRAGMA application_id=1')
            elif failure == 'schema':
                db.execute('CREATE TABLE surplus(value TEXT)')
            elif failure == 'extra-metadata':
                db.execute("INSERT INTO metadata VALUES('unexpected','null')")
            else:
                db.execute('PRAGMA ignore_check_constraints=ON')
                db.execute('UPDATE assignments SET bank_cost=-1 WHERE seq=0')
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        changed = payload.with_name(f'source.{digest}.sqlite')
        payload.rename(changed)
        manifest.update(artifact=changed.name, store_sha256=digest, store_bytes=changed.stat().st_size)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):
        storage.copy_chimew_bank_channel_report_storage(path, output)
    assert output.read_bytes() == before and old_store.read_bytes() == old_payload
    assert not list(tmp_path.glob('*.stage.*'))
    assert not list(tmp_path.glob('.chimew-bank-*'))


def test_invalid_old_selector_has_no_authority_to_delete_an_unrelated_file(tmp_path):
    with create(tmp_path, 'source') as source:
        output, victim = tmp_path / 'destination.json', tmp_path / 'unrelated.sqlite'
        victim.write_bytes(b'keep unrelated data')
        output.write_text(json.dumps({'schema': storage.REPORT_STORAGE_SCHEMA, 'artifact': victim.name}))
        storage.copy_chimew_bank_channel_report_storage(source.manifest_path, output)
        assert victim.read_bytes() == b'keep unrelated data'
        with storage.read_chimew_bank_channel_report(output) as copied:
            assert copied.logical_sha256 == source.logical_sha256


def test_copy_rejects_payload_symlinks_without_touching_the_destination(tmp_path):
    with create(tmp_path, 'source') as source:
        path = source.manifest_path
        manifest = source.manifest
        payload = source.store_path
    real = payload.with_suffix('.retained')
    payload.rename(real)
    payload.symlink_to(real)
    output = tmp_path / 'destination.json'
    output.write_bytes(b'old destination')
    with pytest.raises(ValidationError, match='ownership'):
        storage.copy_chimew_bank_channel_report_storage(path, output)
    assert output.read_bytes() == b'old destination' and real.is_file()


def test_retired_raw_report_is_rejected_by_reader_and_transfer(tmp_path):
    with create(tmp_path, 'source') as source:
        raw = source.materialize()
    path = tmp_path / 'raw.json'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValidationError, match='typed selector'):
        storage.read_chimew_bank_channel_report(path)
    with pytest.raises(ValidationError):
        storage.copy_chimew_bank_channel_report_storage(path, tmp_path / 'destination.json')


def test_report_reader_delegates_complete_validation_without_python_row_or_hash_loops(tmp_path):
    with create(tmp_path, 'source') as source:
        path, logical, count = source.manifest_path, source.logical_sha256, len(source['assignments'])
    with (
        patch.object(storage._AssignmentSequence, '__iter__', side_effect=AssertionError('Python row scan')),
        patch.object(storage, '_logical_sha256', side_effect=AssertionError('Python report hash walk')),
        patch.object(storage, '_connect', side_effect=AssertionError('Python validation database')),
    ):
        with storage.read_chimew_bank_channel_report(path) as checked:
            assert checked.logical_sha256 == logical and len(checked['assignments']) == count


def test_read_only_published_owner_needs_no_writable_scratch_in_its_directory(tmp_path):
    owner = tmp_path / 'published'
    owner.mkdir()
    with create(owner, 'source') as source:
        path, logical = source.manifest_path, source.logical_sha256
    before, mode = set(owner.iterdir()), owner.stat().st_mode
    temporary = storage.tempfile.TemporaryDirectory
    def outside_owner(*args, **kwargs):
        directory = kwargs.get('dir')
        assert directory is None or Path(directory).resolve() != owner.resolve()
        return temporary(*args, **kwargs)
    owner.chmod(0o555)
    try:
        with patch.object(storage.tempfile, 'TemporaryDirectory', side_effect=outside_owner):
            with storage.read_chimew_bank_channel_report(path) as checked:
                assert checked.logical_sha256 == logical
        assert set(owner.iterdir()) == before
    finally:
        owner.chmod(mode)


@pytest.mark.parametrize('change,accepted', [(1e-10, True), (1e-3, False)])
def test_aggregate_physical_costs_keep_the_existing_floating_precision_contract(tmp_path, change, accepted):
    from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage
    with create(tmp_path, 'source') as source:
        value = source.materialize()
    value['metrics']['stage1_physical_site_cost'] += change
    path = tmp_path / 'resealed.json'
    with write_chimew_bank_channel_report_storage(path, value):
        pass
    if accepted:
        with storage.read_chimew_bank_channel_report(path) as checked:
            assert checked['metrics'] == value['metrics']
    else:
        with pytest.raises(ValidationError, match='aggregate costs disagree'):
            storage.read_chimew_bank_channel_report(path)
