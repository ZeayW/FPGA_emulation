import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.global_sta import _PathMetadataBuilder, _IndexedSystemTimingPaths, build_opensta_timing
from emuflow.io import file_sha256, read_json
from emuflow.local_path_timing import path_id_set_sha256
from emuflow.system_timing_storage import (
    SCHEMA, IndexedSystemTimingPaths, write_system_qor, validate_system_qor,
)


def result(*, all_eliminated=False, count=3):
    builder = _PathMetadataBuilder()
    rows = []
    ids = [f"p{i:06d}" for i in range(count)]
    for i, path in enumerate(ids):
        eliminated = all_eliminated or i == count-1
        builder.append(path, representative_path=path, path_scope="same-fpga-local" if i % 2 else "cross-fpga",
                       clock_domain="core", target_period_ns=10, physical_logic_segments_cone_bound=bool(i % 2),
                       interconnect_delay_model="board-link-timing-db" if i % 2 == 0 else None,
                       optimization_source="s3" if eliminated else None, optimization_fpga="fpga0" if eliminated else None)
        if not eliminated:
            for role, required in (("target", 10), ("runtime", 100)):
                rows.append(dict(path=path, role=role, event="capture", arrival_ns=12+i,
                                 required_ns=required, slack_ns=required-12-i))
    metadata = dict(paths=builder.finish(), source_binding={"original_paths": count, "original_path_ids_sha256": path_id_set_sha256(ids)},
                    compressed_representative_paths=count)
    timing = build_opensta_timing({"virtual_dut_clocks": {"core": {"nominal_period_ns": 100}}}, metadata, rows)
    return {"schema": "emuflow.qor-report/v4", "status": timing["status"], "design": "fixture", "timing": timing}


@pytest.mark.parametrize("all_eliminated", [False, True])
def test_typed_results_roundtrip_null_dispositions_and_original_coverage(tmp_path, all_eliminated):
    qor = result(all_eliminated=all_eliminated)
    assert isinstance(qor["timing"]["paths"], _IndexedSystemTimingPaths)
    assert qor["timing"]["paths"].typed_payload_bytes == 40*3
    selector = write_system_qor(tmp_path/"qor_report.json", qor)
    assert selector["schema"] == SCHEMA
    assert "paths" not in selector["header"]["timing"]
    with sqlite3.connect(tmp_path/selector["artifact"]) as db:
        assert db.execute("SELECT delay,target_slack,runtime_slack FROM paths WHERE optimization_source IS NOT NULL").fetchall() == [(None, None, None)]*(3 if all_eliminated else 1)
    loaded = read_json(tmp_path/"qor_report.json")
    assert isinstance(loaded["timing"]["paths"], IndexedSystemTimingPaths)
    assert loaded == qor
    assert loaded["timing"]["paths"][-1] == qor["timing"]["paths"][-1]
    assert validate_system_qor(loaded)["status"] == "pass"
    if all_eliminated:
        assert loaded["timing"]["target_clock"]["worst_slack_bound_ns"] is None
        assert loaded["timing"]["global_opensta"]["authority"] == "no-numerical-checks"
    loaded["timing"]["paths"].close()


def test_summary_reads_do_not_open_or_hash_path_owner(tmp_path, monkeypatch):
    import emuflow.system_timing_storage as storage
    write_system_qor(tmp_path/"qor_report.json", result())
    def forbidden(*args, **kwargs):
        raise AssertionError("bounded summary touched the path payload")
    monkeypatch.setattr(storage.sqlite3, "connect", forbidden)
    monkeypatch.setattr(storage, "file_sha256", forbidden)
    loaded = read_json(tmp_path/"qor_report.json")
    assert loaded["timing"]["target_clock"]["tns_bound_ns"] == -5
    assert len(loaded["timing"]["paths"]) == 3


def test_deterministic_publication_and_directory_relocation(tmp_path):
    qor = result()
    a = write_system_qor(tmp_path/"a/qor_report.json", qor)
    b = write_system_qor(tmp_path/"b/qor_report.json", qor)
    assert a == b
    moved = tmp_path/"moved?with%characters"
    shutil.copytree(tmp_path/"a", moved)
    shutil.rmtree(tmp_path/"a")
    loaded = read_json(moved/"qor_report.json")
    assert loaded == qor
    assert validate_system_qor(loaded)["logical_sha256"] == a["logical_sha256"]
    loaded["timing"]["paths"].close()


def refresh_file_seal(path):
    selector = json.loads(path.read_text())
    payload = path.parent/selector["artifact"]
    selector.update(store_sha256=file_sha256(payload), store_bytes=payload.stat().st_size)
    path.write_text(json.dumps(selector))


@pytest.mark.parametrize("mutation,match", [
    ("DELETE FROM paths WHERE seq=1", "population"),
    ("UPDATE paths SET seq=50 WHERE seq=1", "population"),
    ("CREATE TABLE unowned(secret TEXT)", "envelope"),
    ("UPDATE metadata SET value='another report' WHERE key='header'", "different report"),
    ("PRAGMA user_version=2", "envelope"),
    ("PRAGMA ignore_check_constraints=ON; UPDATE paths SET delay=0 WHERE optimization_source IS NOT NULL", "numerical|scalars"),
])
def test_checked_schema_counts_nulls_and_owner_cannot_be_bypassed_by_refreshing_file_hash(tmp_path, mutation, match):
    path = tmp_path/"qor_report.json"
    selector = write_system_qor(path, result())
    with sqlite3.connect(tmp_path/selector["artifact"]) as db:
        db.executescript(mutation)
    refresh_file_seal(path)
    loaded = read_json(path)
    with pytest.raises(ValidationError, match=match):
        validate_system_qor(loaded)
    loaded["timing"]["paths"].close()


def test_explicit_validation_checks_logical_scalar_seal(tmp_path):
    path = tmp_path/"qor_report.json"
    selector = write_system_qor(path, result())
    with sqlite3.connect(tmp_path/selector["artifact"]) as db:
        db.execute("UPDATE paths SET delay=delay+1 WHERE optimization_source IS NULL")
    refresh_file_seal(path)
    loaded = read_json(path)
    with pytest.raises(ValidationError, match="logical row seal"):
        validate_system_qor(loaded)
    loaded["timing"]["paths"].close()


def test_different_run_payload_cannot_be_substituted_even_with_its_file_hash(tmp_path):
    a = write_system_qor(tmp_path/"a/qor_report.json", result())
    other = result();other["design"] = "another-run"
    b = write_system_qor(tmp_path/"b/qor_report.json", other)
    shutil.copyfile(tmp_path/"b"/b["artifact"], tmp_path/"a"/a["artifact"])
    refresh_file_seal(tmp_path/"a/qor_report.json")
    loaded = read_json(tmp_path/"a/qor_report.json")
    with pytest.raises(ValidationError, match="different report"):
        loaded["timing"]["paths"][0]
    loaded["timing"]["paths"].close()


@pytest.mark.parametrize("name", ["../outside.sqlite", "/absolute.sqlite", "a\\b.sqlite"])
def test_payload_reference_must_be_a_sibling(tmp_path, name):
    path = tmp_path/"qor_report.json"
    selector = write_system_qor(path, result())
    selector["artifact"] = name
    path.write_text(json.dumps(selector))
    with pytest.raises(ValidationError, match="selector"):
        read_json(path)


def test_publication_failure_does_not_leave_unpublished_payload(tmp_path, monkeypatch):
    import emuflow.system_timing_storage as storage
    def fail(*args, **kwargs):
        raise OSError("publication failure")
    monkeypatch.setattr(storage, "write_json", fail)
    with pytest.raises(OSError, match="publication failure"):
        write_system_qor(tmp_path/"qor_report.json", result())
    assert list(tmp_path.iterdir()) == []


def test_missing_original_measurements_and_interleaved_indexed_bundles_fail_closed():
    builder = _PathMetadataBuilder()
    for path in ("p", "q"):
        builder.append(path, representative_path=path, path_scope="cross-fpga", clock_domain="core",
                       target_period_ns=10, physical_logic_segments_cone_bound=False)
    metadata = dict(paths=builder.finish(), source_binding=None, compressed_representative_paths=2)
    runtime = {"virtual_dut_clocks": {"core": {"nominal_period_ns": 100}}}
    rows = [dict(path=path, role=role, event="capture", arrival_ns=1, required_ns=10, slack_ns=9)
            for path, role in (("p", "target"), ("q", "target"), ("p", "runtime"), ("q", "runtime"))]
    with pytest.raises(ValidationError, match="contiguous original-path"):
        build_opensta_timing(runtime, metadata, rows)
    with pytest.raises(ValidationError, match="measurement population"):
        build_opensta_timing(runtime, metadata, (rows[0], rows[2]))


def test_large_path_population_keeps_summary_bounded_and_scalar_columns_typed(tmp_path):
    qor = result(count=25000)
    paths = qor["timing"]["paths"]
    assert paths.typed_payload_bytes == 40*25000
    assert not isinstance(paths, list)
    selector = write_system_qor(tmp_path/"qor_report.json", qor)
    assert (tmp_path/"qor_report.json").stat().st_size < 10000
    assert selector["store_bytes"] < 250*25000
    loaded = read_json(tmp_path/"qor_report.json")
    assert loaded["timing"]["paths"][12345] == paths[12345]
    assert validate_system_qor(loaded)["paths"] == 25000
    loaded["timing"]["paths"].close()


def test_publisher_rejects_report_metrics_that_disagree_with_engine_rows(tmp_path):
    qor = result()
    qor["timing"]["target_clock"]["tns_bound_ns"] = 0
    with pytest.raises(ValidationError, match="clock metrics"):
        write_system_qor(tmp_path/"qor_report.json", qor)
    assert list(tmp_path.iterdir()) == []


def test_payload_digest_is_checked_when_paths_are_requested(tmp_path):
    path = tmp_path/"qor_report.json"
    selector = write_system_qor(path, result())
    payload = tmp_path/selector["artifact"]
    with payload.open("r+b") as stream:
        stream.seek(-1, 2);value = stream.read(1);stream.seek(-1, 2);stream.write(bytes([value[0]^1]))
    loaded = read_json(path)
    assert loaded["timing"]["target_clock"]["tns_bound_ns"] == -5
    with pytest.raises(ValidationError, match="seal"):
        loaded["timing"]["paths"][0]


def test_original_id_lookup_uses_the_unique_index_and_detects_missing_ids(tmp_path):
    path = tmp_path/"qor_report.json"
    write_system_qor(path, result(count=2500))
    rows = read_json(path)["timing"]["paths"]
    db = rows._open()
    # A full table walk exceeds this VM budget, while the unique path lookup
    # and the bounded symbol joins remain well below it.
    db.set_progress_handler(lambda: 1, 500)
    assert rows.by_path("p002345")["path"] == "p002345"
    with pytest.raises(KeyError):
        rows.by_path("missing")
    rows.close()


def test_flow_manifest_retains_the_canonical_payload_seal_without_rehash(tmp_path, monkeypatch):
    from emuflow.multi_fpga_flow import _runtime_timing_artifacts
    import emuflow.system_timing_storage as storage
    path = tmp_path/"runtime/qor_report.json"
    selector = write_system_qor(path, result())
    def forbidden(*args, **kwargs):
        raise AssertionError("producer rehashed its just-published path payload")
    monkeypatch.setattr(storage, "file_sha256", forbidden)
    artifacts = _runtime_timing_artifacts(path.parent, tmp_path, {"artifacts": {"system_timing_paths": selector["artifact"]}})
    assert artifacts["system_timing_paths"] == {"path": "runtime/"+selector["artifact"], "sha256": selector["store_sha256"]}


def test_unicode_original_ids_and_clock_names_roundtrip_without_changing_the_id_seal(tmp_path):
    identity = '路径/α"'
    builder = _PathMetadataBuilder()
    builder.append(identity, representative_path=identity, path_scope="same-fpga-local", clock_domain="时钟",
                   target_period_ns=10, physical_logic_segments_cone_bound=False,
                   optimization_source="s2", optimization_fpga="fpga0")
    timing = build_opensta_timing({"virtual_dut_clocks": {"时钟": {"nominal_period_ns": 100}}},
        dict(paths=builder.finish(), source_binding={"original_paths": 1, "original_path_ids_sha256": path_id_set_sha256([identity])}, compressed_representative_paths=1), ())
    qor = dict(schema="emuflow.qor-report/v4", timing=timing)
    write_system_qor(tmp_path/"qor_report.json", qor)
    loaded = read_json(tmp_path/"qor_report.json")
    assert loaded["timing"]["paths"].by_path(identity)["clock_domain"] == "时钟"
    assert validate_system_qor(loaded)["status"] == "pass"
    loaded["timing"]["paths"].close()
