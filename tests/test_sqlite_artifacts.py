"""Exercise real SQLite URI semantics, including builds without USE_URI."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from emuflow.sqlite_artifacts import immutable_sqlite_uri
from tests.native_build import emuflow_native, tlr_router


def test_immutable_uri_preserves_path_bytes_and_rejects_writes(tmp_path):
    path = tmp_path / "源 ?#%&.sqlite"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE original(value TEXT)")
        source.execute("INSERT INTO original VALUES ('expected')")
        source.commit()
    with closing(sqlite3.connect(":memory:", uri=True)) as consumer:
        consumer.execute("ATTACH DATABASE ? AS source", (immutable_sqlite_uri(path),))
        assert consumer.execute("SELECT value FROM source.original").fetchone() == ("expected",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            consumer.execute("DELETE FROM source.original")
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_immutable_uri_does_not_create_missing_source(tmp_path):
    path = tmp_path / "missing ?#%.sqlite"
    with closing(sqlite3.connect(":memory:", uri=True)) as consumer:
        with pytest.raises(sqlite3.OperationalError, match="unable to open"):
            consumer.execute("ATTACH DATABASE ? AS source", (immutable_sqlite_uri(path),))
    assert not list(tmp_path.iterdir())


def test_native_route_publication_without_sqlite_global_uri_default():
    # Configure only an isolated child before any connection exists. This
    # reproduces the Linux failure even on a host built with SQLITE_USE_URI=1.
    # Run the actual native producer and SQL publisher, never a mock database.
    program = r'''
import ctypes
import _sqlite3
import sqlite3
import sys
from pathlib import Path
library = ctypes.CDLL(_sqlite3.__file__)
assert library.sqlite3_shutdown() == 0
assert library.sqlite3_config(17, 0) == 0  # SQLITE_CONFIG_URI
import tests.native_build as native
native.emuflow_native = lambda: Path(sys.argv[1])
native.tlr_router = lambda: Path(sys.argv[2])
import pytest
sys.exit(pytest.main(["-q", "tests/test_phase4_native.py::test_complete_native_stage_preserves_records_hashes_and_independent_checks[False-False-False]"]))
'''
    result = subprocess.run(
        [sys.executable, "-c", program, str(emuflow_native()), str(tlr_router())],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
