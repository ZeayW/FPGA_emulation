"""The offline I/O oracle exercises the production API, never another flow."""

import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import os
import sqlite3
from copy import deepcopy
from contextlib import closing

import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.phase3_assignment_index import write_phase3_assignment_index
from tests.native_build import emuflow_native
from tests.schedule_store_fixture import write_tdm_schedule_storage


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "phase6_publication_qualification",
    ROOT / "scripts/validation/qualify_phase6_publication.py",
)
QUALIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALIFY)


def prepare(root, monkeypatch):
    from tests.test_phase6 import Phase6Test

    case = Phase6Test()
    case.setUp()
    ir, assignment, schedule, platform = (
        root / name
        for name in (
            "design.emuir.sqlite",
            "assignment.sqlite",
            "schedule.json",
            "platform.json",
        )
    )
    write_compact_emuir(ir, case.ir.to_dict())
    write_phase3_assignment_index(assignment, case.ir, case.assignment)
    (root / "assignment.json").write_text(json.dumps(case.assignment))
    write_tdm_schedule_storage(schedule, case.schedule)
    platform.write_text(json.dumps(case.platform.to_dict()))
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    # An explicit unit-test installation; deployed qualification requires the
    # real runtime's exact-source manifest, checked by the same code below.
    runtime = root / "fixture-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/emuflow-native").symlink_to(emuflow_native())
    (runtime / "SOURCE_COMMIT").write_text(commit + "\n")
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(runtime))
    return {
        "schema": "emuflow.phase6-publication-qualification/v1",
        "source_commit": commit,
        "workspace": str(root),
        "clock_periods": {clock["id"]: 10.0 for clock in case.ir.value["clocks"]},
        "expected_design": case.ir.value["design"]["name"],
        "expected_platform": case.platform.name,
        "minimum_instances": 1,
        "expected_fpgas": sorted(fpga.id for fpga in case.platform.fpgas),
        "inputs": {
            "ir": str(ir),
            "assignment": str(assignment),
            "schedule": str(schedule),
            "platform": str(platform),
        },
    }


def test_both_qualification_modes_run_the_same_complete_api(tmp_path, monkeypatch):
    request = prepare(tmp_path, monkeypatch)
    results = []
    for mode in ("python-reference", "native"):
        path = tmp_path / f"{mode}.request.json"
        path.write_text(
            json.dumps({**request, "mode": mode, "output": str(tmp_path / mode)})
        )
        result = QUALIFY.qualify(path, tmp_path / f"{mode}.result.json")
        assert result["status"] == "pass" and result["inputs_unchanged"]
        assert result["api_wall_seconds"] > 0
        assert result["design"] == request["expected_design"]
        assert len(result["publications"]) == 2
        assert all(
            ("native_metrics" in item) == (mode == "native")
            for item in result["publications"]
        )
        assert result["record_seal_wall_seconds"] > 0
        assert set(result["artifact_seals"]) == {"split", "transport"}
        results.append(json.loads((tmp_path / f"{mode}.result.json").read_text()))
        # Sequential qualification retains full-record seals, not both payloads.
        shutil.rmtree(tmp_path / mode)
    assert all(
        item["logical_identity"] for item in QUALIFY.compare_results(*results).values()
    )
    assert all(
        item["file_identity"] for item in QUALIFY.compare_results(*results).values()
    )
    for key, value in (
        ("input_generations", {}),
        ("generated_text", {}),
        ("clock_periods", {"different": 10}),
    ):
        changed = deepcopy(results[1])
        changed[key] = value
        with pytest.raises(ValidationError, match="configuration differs"):
            QUALIFY.compare_results(results[0], changed)
    changed = deepcopy(results[1])
    changed["artifact_seals"]["transport"]["tables"][0]["records"] += 1
    with pytest.raises(ValidationError, match="records differ"):
        QUALIFY.compare_results(results[0], changed)
    changed = deepcopy(results[1])
    del changed["artifact_seals"]["split"]
    with pytest.raises(ValidationError, match="complete typed record seals"):
        QUALIFY.compare_results(results[0], changed)


def test_relation_comparison_accepts_page_layout_changes_and_rejects_record_changes(
    tmp_path,
):
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    with closing(sqlite3.connect(first)) as db, db:
        db.execute("CREATE TABLE records(seq INTEGER PRIMARY KEY,value TEXT)")
        db.executemany("INSERT INTO records VALUES (?,?)", [(1, "one"), (2, "two")])
    shutil.copyfile(first, second)
    with closing(sqlite3.connect(second)) as db:
        db.execute("PRAGMA page_size=16384")
        db.execute("VACUUM")
    result = QUALIFY.compare_sqlite_records(first, second)
    assert not result["file_identity"] and result["logical_identity"]
    with closing(sqlite3.connect(second)) as db, db:
        db.execute("UPDATE records SET value='different' WHERE seq=2")
    with pytest.raises(ValidationError, match="records differ"):
        QUALIFY.compare_sqlite_records(first, second)


def _typed_relation(path, values):
    with closing(sqlite3.connect(path)) as db, db:
        # No affinity: SQLite must preserve the supplied storage types.
        db.execute("CREATE TABLE records(seq INTEGER PRIMARY KEY, a, b)")
        db.executemany("INSERT INTO records VALUES (?,?,?)", values)


def _independent_sql_identity(first, second):
    with closing(sqlite3.connect(first)) as db:
        db.execute("ATTACH DATABASE ? AS other", (str(second),))
        fields = "seq, typeof(a), a COLLATE BINARY, typeof(b), b COLLATE BINARY"
        return all(
            db.execute(
                f"SELECT {fields} FROM {left}.records EXCEPT "
                f"SELECT {fields} FROM {right}.records LIMIT 1"
            ).fetchone()
            is None
            for left, right in (("main", "other"), ("other", "main"))
        )


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ((1, None), (1.0, None), False),
        ((1, None), ("1", None), False),
        (("a", None), (b"a", None), False),
        ((None, b""), ("", b""), False),
        (("ab", "c"), ("a", "bc"), False),
        (("x\0y", "雪"), ("x\0y", "雪"), True),
        ((b"\0\xffRNT", b""), (b"\0\xffRNT", b""), True),
        ((-0.0, float("inf")), (0.0, float("inf")), True),
        ((-(2**63), 2**63 - 1), (-(2**63), 2**63 - 1), True),
        ((1.25, None), (1.2500000000000002, None), False),
    ],
)
def test_record_seals_match_independent_sql_type_and_binary_field_comparison(
    tmp_path,
    left,
    right,
    equal,
):
    first, second = tmp_path / "one.sqlite", tmp_path / "two.sqlite"
    _typed_relation(first, [(1, *left), (2, "unchanged", b"unchanged")])
    _typed_relation(second, [(2, "unchanged", b"unchanged"), (1, *right)])
    assert _independent_sql_identity(first, second) is equal
    if equal:
        assert QUALIFY.compare_sqlite_records(first, second)["logical_identity"]
    else:
        with pytest.raises(ValidationError, match="records differ"):
            QUALIFY.compare_sqlite_records(first, second)


def test_seal_orders_composite_keys_and_includes_every_user_table(tmp_path):
    paths = [tmp_path / "one.sqlite", tmp_path / "two.sqlite"]
    rows = [("a", 2, "second"), ("A", 1, "first"), ("雪", 0, "last")]
    for index, path in enumerate(paths):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE records(a TEXT, b INTEGER, v TEXT, PRIMARY KEY(b,a))"
            )
            db.execute('CREATE TABLE "sqliteX"(k INTEGER PRIMARY KEY, v BLOB)')
            db.executemany(
                "INSERT INTO records VALUES (?,?,?)", rows[:: 1 if index else -1]
            )
            db.execute('INSERT INTO "sqliteX" VALUES (1,?)', (b"one",))
    assert QUALIFY.compare_sqlite_records(*paths)["logical_identity"]
    with closing(sqlite3.connect(paths[1])) as db, db:
        db.execute('UPDATE "sqliteX" SET v=? WHERE k=1', (b"two",))
    with pytest.raises(ValidationError, match="records differ"):
        QUALIFY.compare_sqlite_records(*paths)


@pytest.mark.parametrize(
    "change",
    [
        "PRAGMA application_id=42",
        "PRAGMA user_version=3",
        "ALTER TABLE records ADD COLUMN extra BLOB",
        "CREATE TABLE another(k INTEGER PRIMARY KEY)",
        "DELETE FROM records WHERE seq=1",
        "UPDATE records SET seq=7 WHERE seq=1",
    ],
)
def test_record_seals_reject_changed_schema_identity_or_row_population(
    tmp_path, change
):
    first, second = tmp_path / "one.sqlite", tmp_path / "two.sqlite"
    _typed_relation(first, [(1, "one", None)])
    shutil.copyfile(first, second)
    with closing(sqlite3.connect(second)) as db, db:
        db.execute(change)
    with pytest.raises(ValidationError, match="records differ"):
        QUALIFY.compare_sqlite_records(first, second)


@pytest.mark.parametrize(
    ("ddl", "insert", "message"),
    [
        ("CREATE TABLE records(k TEXT, v TEXT)", None, "declared primary"),
        (
            "CREATE TABLE records(k TEXT PRIMARY KEY, v TEXT)",
            'INSERT INTO records VALUES (NULL,"one")',
            "non-null primary",
        ),
        (
            "CREATE TABLE records(k INTEGER PRIMARY KEY, v AS (k+1))",
            None,
            "generated/hidden",
        ),
    ],
)
def test_record_seal_rejects_unsupported_or_nullable_identities(
    tmp_path, ddl, insert, message
):
    path = tmp_path / "one.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(ddl)
        if insert:
            db.execute(insert)
    with pytest.raises(ValidationError, match=message):
        QUALIFY.seal_sqlite_records(path)


@pytest.mark.parametrize("during", ["hash", "records"])
def test_record_seal_rejects_input_replacement_during_verification(
    tmp_path, monkeypatch, during
):
    path, replacement = tmp_path / "one.sqlite", tmp_path / "replacement.sqlite"
    _typed_relation(path, [(1, "one", None), (2, "two", None)])
    _typed_relation(replacement, [(1, "different", None)])
    name = "file_sha256" if during == "hash" else "_record_bytes"
    original = getattr(QUALIFY, name)

    def replace(*args):
        value = original(*args)
        if replacement.exists():
            os.replace(replacement, path)
        return value

    monkeypatch.setattr(QUALIFY, name, replace)
    with pytest.raises(ValidationError, match="generation changed"):
        QUALIFY.seal_sqlite_records(path)


@pytest.mark.parametrize("change", ["schema", "digest", "column", "pk", "count"])
def test_record_seal_rejects_malformed_evidence(tmp_path, change):
    path = tmp_path / "one.sqlite"
    _typed_relation(path, [(1, "one", None)])
    original = QUALIFY.seal_sqlite_records(path)
    changed = deepcopy(original)
    if change == "schema":
        changed["schema"] = "unknown"
    elif change == "digest":
        changed["tables"][0]["records_sha256"] = "x" * 64
    elif change == "column":
        changed["tables"][0]["columns"][0][0] = 10
    elif change == "pk":
        changed["tables"][0]["columns"][0][5] = 0
    else:
        changed["tables"][0]["records"] = True
    with pytest.raises(ValidationError, match="malformed"):
        QUALIFY.compare_sqlite_seals(original, changed)


def test_workspace_measurement_deduplicates_aliases_and_excludes_borrowed_symlinks(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    borrowed = tmp_path / "borrowed"
    borrowed.write_bytes(b"b" * 8192)
    (workspace / "borrowed").symlink_to(borrowed)
    owned = workspace / "owned"
    with owned.open("w+b") as stream:
        stream.write(b"a" * 4096)
        stream.flush()
        alias = workspace / "alias"
        os.link(owned, alias)
        assert QUALIFY.workspace_bytes(workspace)[0] == 4096
        owned.unlink()
        if Path("/proc/self/fd").is_dir():
            alias.unlink()
            assert QUALIFY.workspace_bytes(workspace)[0] == 4096


def test_native_failure_never_selects_the_reference_publisher(tmp_path, monkeypatch):
    from emuflow import split_netlist_storage

    request = prepare(tmp_path, monkeypatch)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps({**request, "mode": "native", "output": str(tmp_path / "native")})
    )

    def fail(*args, **kwargs):
        raise ValidationError("injected native publisher failure")

    monkeypatch.setattr(split_netlist_storage, "publish_sqlite_store", fail)
    result_path = tmp_path / "result.json"
    with pytest.raises(ValidationError, match="injected native publisher failure"):
        QUALIFY.qualify(path, result_path)
    assert not result_path.exists()


def test_qualification_rejects_wrong_source_without_starting_a_flow(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "emuflow.phase6-publication-qualification/v1",
                "mode": "native",
                "source_commit": "0" * 40,
            }
        )
    )
    with pytest.raises(ValidationError, match="source commit"):
        QUALIFY.qualify(request, tmp_path / "result.json")
