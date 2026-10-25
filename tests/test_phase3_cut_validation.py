"""Independent typed cut validation, canonical identity and rejection gates."""

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.phase3_assignment_index import (
    _iter_cut_records,
    validate_phase3_assignment_index,
    write_phase3_assignment_index,
)
from emuflow.phase3_assignment_native import validate_native_assignment_cuts
from tests.native_build import emuflow_native
from tests.test_phase3_assignment_direct_cuts import _assignment
from tests.test_phase3_clusters_native import source


def published(source, tmp_path, mode="normal"):
    with _assignment(source, "normal" if mode == "nonsemantic" else mode) as (
        ir,
        assignment,
        _typed,
    ):
        if mode == "nonsemantic":
            assignment.pop("semantic_contract")
            assignment.pop("semantic_contract_sha256")
            assignment["cut_nets"] = [
                dict(
                    {
                        k: v
                        for k, v in row.items()
                        if k
                        not in {
                            "dependency_level",
                            "combinational_dependency_depth",
                            "predecessor_cut_nets",
                        }
                    },
                    transport_round=7,
                )
                for row in assignment["cut_nets"]
            ]
        path = tmp_path / "published #?雪" / "assignment.sqlite"
        write_phase3_assignment_index(path, ir, assignment)
        return path


def reference_identity(path):
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path)) as db:
        rows = list(_iter_cut_records(db))
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return len(rows), sum(len(row["sink_fpgas"]) for row in rows), digest.hexdigest()


@pytest.mark.parametrize(
    "mode", ["normal", "unicode", "sparse", "no-cuts", "nonsemantic"]
)
def test_native_validation_matches_independent_host_identity_without_host_expansion(
    source, tmp_path, mode
):
    path = published(source, tmp_path, mode)
    expected = validate_phase3_assignment_index(path)
    count, sinks, digest = reference_identity(path)
    original = path.read_bytes()
    with patch(
        "emuflow.phase3_assignment_index._iter_cut_records",
        side_effect=AssertionError("host cut expansion"),
    ):
        with patch(
            "emuflow.phase3_assignment_index._json",
            side_effect=AssertionError("host canonical cut JSON"),
        ):
            assert validate_phase3_assignment_index(path) == expected
            assert validate_native_assignment_cuts(path) == {
                "schema": "emuflow.assignment-cut-validation/v1",
                "status": "pass",
                "records": count,
                "sinks": sinks,
                "sha256": digest,
            }
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "kind",
    [
        "count",
        "digest",
        "schema",
        "source",
        "sink",
        "sink-source",
        "no-sinks",
        "orphan-sink",
        "orphan-predecessor",
        "missing-predecessor",
        "self-predecessor",
        "predecessor-order",
        "incomplete-dependency",
        "missing-dependencies",
        "negative-endpoints",
        "negative-depth",
        "negative-round",
        "scalar-type",
        "self-sink",
        "empty-class",
        "net-order",
        "part-order",
        "version",
        "utf8",
    ],
)
def test_read_only_validator_rejects_corruption_and_preserves_prior_summary(
    source, tmp_path, kind
):
    path = published(source, tmp_path)
    with closing(sqlite3.connect(path)) as db, db:
        net, owner = db.execute(
            "SELECT net_id,source_part_index FROM cuts WHERE cut_index=0"
        ).fetchone()
        if kind in ("count", "digest", "schema"):
            key, value = {
                "count": ("cut_records", "999999"),
                "digest": ("cut_records_sha256", json.dumps("0" * 64)),
                "schema": ("cut_records_schema", json.dumps("wrong/v1")),
            }[kind]
            db.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
        elif kind == "source":
            db.execute("UPDATE cuts SET source_part_index=900 WHERE cut_index=0")
        elif kind == "sink":
            db.execute(
                "UPDATE cut_sinks SET sink_part_index=900 WHERE net_id=?", (net,)
            )
        elif kind == "sink-source":
            db.execute(
                "UPDATE cut_sinks SET source_part_index=900 WHERE net_id=?", (net,)
            )
        elif kind == "no-sinks":
            db.execute("DELETE FROM cut_sinks WHERE net_id=?", (net,))
        elif kind == "orphan-sink":
            db.execute("INSERT INTO cut_sinks VALUES('missing',0,1)")
        elif kind == "orphan-predecessor":
            db.execute("INSERT INTO cut_predecessors VALUES(900,0,?)", (net,))
        elif kind == "missing-predecessor":
            db.execute("DELETE FROM cut_predecessors WHERE cut_index=0")
            db.execute("INSERT INTO cut_predecessors VALUES(0,0,'missing')")
        elif kind == "self-predecessor":
            db.execute("DELETE FROM cut_predecessors WHERE cut_index=0")
            db.execute("INSERT INTO cut_predecessors VALUES(0,0,?)", (net,))
        elif kind == "predecessor-order":
            other = db.execute("SELECT net_id FROM cuts WHERE cut_index=1").fetchone()[
                0
            ]
            db.execute("DELETE FROM cut_predecessors WHERE cut_index=0")
            db.execute("INSERT INTO cut_predecessors VALUES(0,4,?)", (other,))
        elif kind == "incomplete-dependency":
            db.execute(
                "UPDATE cuts SET combinational_dependency_depth=NULL WHERE cut_index=0"
            )
        elif kind == "missing-dependencies":
            db.execute("DELETE FROM cut_predecessors")
            db.execute(
                "UPDATE cuts SET dependency_level=NULL,combinational_dependency_depth=NULL"
            )
        elif kind in ("negative-endpoints", "negative-depth", "negative-round"):
            column = {
                "negative-endpoints": "sink_endpoints",
                "negative-depth": "combinational_dependency_depth",
                "negative-round": "transport_round",
            }[kind]
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE cuts SET " + column + "=-1 WHERE cut_index=0")
        elif kind == "scalar-type":
            db.execute(
                "UPDATE cuts SET dependency_level='not-integer' WHERE cut_index=0"
            )
        elif kind == "self-sink":
            db.execute(
                "UPDATE cut_sinks SET sink_part_index=? WHERE net_id=?", (owner, net)
            )
        elif kind == "empty-class":
            db.execute("UPDATE cuts SET cut_class='' WHERE cut_index=0")
        elif kind == "net-order":
            db.execute("UPDATE cuts SET net_id='zzzzzz' WHERE cut_index=0")
            db.execute("UPDATE cut_sinks SET net_id='zzzzzz' WHERE net_id=?", (net,))
            db.execute(
                "UPDATE cut_predecessors SET predecessor_net_id='zzzzzz' WHERE predecessor_net_id=?",
                (net,),
            )
        elif kind == "part-order":
            db.execute("UPDATE parts SET part_index=900 WHERE part_index=0")
        elif kind == "version":
            db.execute("PRAGMA user_version=3")
        else:
            db.execute(
                "UPDATE cuts SET cut_class=CAST(x'ff' AS TEXT) WHERE cut_index=0"
            )
    original = path.read_bytes()
    output, report = tmp_path / "prior.json", tmp_path / "stage.json"
    output.write_bytes(b"previous-summary")
    result = subprocess.run(
        [
            str(emuflow_native()),
            "--report",
            str(report),
            "phase3",
            "validate-cuts",
            str(path),
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(report.read_text())["error"]["code"] == "invalid-artifact"
    assert output.read_bytes() == b"previous-summary"
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp.*"))


@pytest.mark.parametrize(
    "kind",
    [
        "schema",
        "count-type",
        "sinks",
        "digest",
        "extra",
        "oversized",
        "invalid-json",
        "source-generation",
    ],
)
def test_native_summary_rejection_has_no_host_fallback(source, tmp_path, kind):
    import emuflow.phase3_assignment_native as native
    from emuflow.tool_runtime import NativeToolProcess

    path = published(source, tmp_path)
    original = path.read_bytes()

    class ChangedResult:
        def __init__(self, executable, command, **kwargs):
            self.inner = NativeToolProcess(executable, command, **kwargs)
            self.output = Path(command[-1])

        def __enter__(self):
            self.inner.__enter__()
            return self

        def finish(self):
            result = self.inner.finish()
            assert result.returncode == 0
            summary = json.loads(self.output.read_text())
            if kind == "source-generation":
                path.chmod(path.stat().st_mode ^ 0o100)
            elif kind == "oversized":
                self.output.write_bytes(b" " * 1025)
            elif kind == "invalid-json":
                self.output.write_text("invalid JSON")
            else:
                key, value = {
                    "schema": ("schema", "wrong/v1"),
                    "count-type": ("records", True),
                    "sinks": ("sinks", 0),
                    "digest": ("sha256", "?" * 64),
                    "extra": ("payload", []),
                }[kind]
                summary[key] = value
                self.output.write_text(json.dumps(summary))
            return result

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    with patch.object(native, "NativeToolProcess", ChangedResult):
        with patch(
            "emuflow.phase3_assignment_index._iter_cut_records",
            side_effect=AssertionError("host fallback"),
        ):
            with pytest.raises(ValidationError):
                validate_phase3_assignment_index(path)
    assert path.read_bytes() == original


def test_large_ordered_cut_relation_has_bounded_host_memory(source, tmp_path):
    """Storage stress only; these records are not a circuit or feature claim."""
    import tracemalloc

    path = published(source, tmp_path)
    count = 100_000
    digest = hashlib.sha256()

    # Construct a declared record population independently from native code.
    # Reference JSON is emitted once, outside the measured production call.
    def records():
        for i in range(count):
            net = f"cut{i:06d}"
            predecessor = [f"cut{i-1:06d}"] if i else []
            row = {
                "net": net,
                "cut_class": "combinational",
                "source_fpgas": ["a"],
                "sink_fpgas": ["b"],
                "sink_endpoints": 3,
                "dependency_level": i,
                "combinational_dependency_depth": i,
                "predecessor_cut_nets": predecessor,
            }
            digest.update(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            )
            digest.update(b"\n")
            yield i, net, 0, "combinational", 3, None, i, i

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM cut_predecessors")
        db.execute("DELETE FROM cut_sinks")
        db.execute("DELETE FROM cuts")
        db.executemany("INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)", records())
        db.executemany(
            "INSERT INTO cut_sinks VALUES(?,0,1)",
            ((f"cut{i:06d}",) for i in range(count)),
        )
        db.executemany(
            "INSERT INTO cut_predecessors VALUES(?,0,?)",
            ((i, f"cut{i-1:06d}") for i in range(1, count)),
        )
        db.execute("UPDATE metadata SET value=? WHERE key='cut_records'", (str(count),))
        db.execute(
            "UPDATE metadata SET value=? WHERE key='cut_records_sha256'",
            (json.dumps(digest.hexdigest()),),
        )
    with patch(
        "emuflow.phase3_assignment_index._iter_cut_records",
        side_effect=AssertionError("host cut population"),
    ):
        tracemalloc.start()
        try:
            summary = validate_native_assignment_cuts(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert summary["records"] == count and summary["sinks"] == count
    assert summary["sha256"] == digest.hexdigest()
    assert peak < 2 * 1024 * 1024
