"""SQLite transaction scopes must release resources before phase retirement."""
from contextlib import closing
import sqlite3

import pytest

from emuflow.common.errors import ValidationError
from emuflow.flow.prepared_inputs import _published_timing_report
from tests.openparf_input_reference import _write_site_database


@pytest.fixture
def retained_connections(monkeypatch):
    original = sqlite3.connect
    connections = []
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        # Deliberately keep the object alive beyond the caller's scope. Merely
        # committing a context or relying on GC cannot satisfy the assertion.
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    return original, connections


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


@pytest.mark.parametrize('invalid_version', [False, True])
def test_prepared_readers_close_on_success_and_rejection(tmp_path, retained_connections, invalid_version):
    original, connections = retained_connections
    with closing(original(tmp_path/'paths.sqlite')) as database, database:
        database.executescript('PRAGMA user_version=1; CREATE TABLE paths(clock_domain TEXT,clock_period_ns REAL); INSERT INTO paths VALUES ("clk",10);')
        if invalid_version: database.execute('PRAGMA user_version=9')
    with closing(original(tmp_path/'weights.sqlite')) as database, database:
        database.executescript('PRAGMA user_version=1; CREATE TABLE net_weights(net TEXT); INSERT INTO net_weights VALUES ("n0");')
    timing = {'design': 'fixture', 'payloads': {'paths': {'path': 'paths.sqlite', 'format': 'sqlite-sta-path/v1', 'records': 1}}, 'summary': {'paths': 1}}
    weights = {'schema': 'emuflow.partition-net-weights/v2', 'design': 'fixture', 'payloads': {'weights': {'path': 'weights.sqlite', 'format': 'sqlite-net-weights/v1', 'records': 1}}, 'summary': {'weighted_nets': 1}}
    if invalid_version:
        with pytest.raises(ValidationError, match='version mismatch'):
            _published_timing_report(tmp_path, timing, weights, {'clk': 10.0})
    else:
        assert _published_timing_report(tmp_path, timing, weights, {'clk': 10.0})['status'] == 'pass'
    assert len(connections) == (1 if invalid_version else 2)
    assert_closed(connections)


@pytest.mark.parametrize('duplicate_site', [False, True])
def test_site_writer_releases_in_memory_and_backup_connections(tmp_path, retained_connections, duplicate_site):
    original, connections = retained_connections
    site = {'dense_x': 0, 'dense_y': 0, 'physical_x': 0, 'physical_y': 0,
            'placement_x': 0.0, 'placement_y': 0.0, 'site': 'S0',
            'resources': {'lut': 1}, 'physical_sites': {'lut': ['S0', 'S0'] if duplicate_site else ['S0']}}
    output = tmp_path/'sites.sqlite'
    if duplicate_site:
        with pytest.raises(sqlite3.IntegrityError):
            _write_site_database(output, {'sites': [site]})
    else:
        assert _write_site_database(output, {'sites': [site]}) == 1
        with closing(original(output)) as reader:
            assert reader.execute('SELECT count(*) FROM sites').fetchone()[0] == 1
    assert len(connections) == (1 if duplicate_site else 2)
    assert_closed(connections)


def test_lazy_name_map_readers_close_after_lookup_exhaustion_and_early_stop(tmp_path, retained_connections):
    from emuflow.physical.openparf.xilinx_openparf_name_map_storage import read_xilinx_openparf_atomic_name_map
    from tests import test_xilinx_openparf_name_map_storage as fixture
    _original, connections = retained_connections
    path = tmp_path/'name_map.json'
    with fixture._write(path):
        pass
    with read_xilinx_openparf_atomic_name_map(path) as view:
        assert view['atoms'][0]['openparf'] == 'a0'
        assert len(list(view['atoms'])) == 2
        assert_closed(connections)
        iterator = iter(view['atoms'])
        assert next(iterator)['openparf'] == 'a0'
        # A live stream still owns its cursor. Early close, rather than GC,
        # must release it before the publication can be retired.
        assert connections[-1].execute('SELECT 1').fetchone()[0] == 1
        iterator.close()
        assert_closed(connections)
