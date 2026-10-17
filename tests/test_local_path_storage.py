import json
import shutil
import sqlite3
import tracemalloc
from unittest import mock

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.local_path_storage import (
    IDENTITY_V1, IDENTITY_V2, TIMING, LocalPathRows, PreparedPathStore,
    open_local_paths, write_local_paths,
)
from emuflow.local_path_timing import (
    path_id_set_sha256, validate_local_path_identity, validate_local_path_timing,
)


def metadata(timing=False, v2=True):
    result = {"schema": TIMING if timing else IDENTITY_V2 if v2 else IDENTITY_V1,
              "status": "pass", "design": "dut", "fpga": "F0", "provider": "test",
              "source": {**{k: "a" * 64 for k in ("path_database_sha256", "original_ir_sha256", "assignment_sha256", "routes_sha256", "original_path_ids_sha256")},
                         "original_paths": 50000}}
    if timing:
        result["identity_schema"] = IDENTITY_V2 if v2 else IDENTITY_V1
    return result


def record(i=0, timing=False, v2=True):
    value = {"id": f"p{i:08d}", "kind": "local", "fpga": "F0", "clock_domain": "clk",
             "clock_period_ns": 10.0, "start_pin": "cell:源/Q", "end_pin": "cell:目标/D"}
    if v2:
        value.update(required_time_ns=9.5, measurement="endpoint-longest-path-fallback", path_pins=[])
    if timing:
        value["delay_ns"] = i / 10 + 0.2
    return value


@pytest.mark.parametrize("timing,v2", [(False, False), (False, True), (True, False), (True, True)])
def test_roundtrip_and_relocation(tmp_path, timing, v2):
    path = tmp_path / "original" / "identity.json"
    rows = [record(i, timing, v2) for i in (2, 0, 1)]
    meta = metadata(timing, v2)
    if v2:
        rows[0].update(measurement="explicit-routed-path-chain", path_pins=[rows[0]["start_pin"], "middle", rows[0]["end_pin"]])
    write_local_paths(path, meta, iter(rows))
    header = json.loads(path.read_text())
    assert "paths" not in header
    value = read_json(path)
    assert isinstance(value["paths"], LocalPathRows)
    assert list(value["paths"]) == sorted(rows, key=lambda r: r["id"])
    expected = {**meta, "coverage": {"local_paths": 3}, "paths": sorted(rows, key=lambda r: r["id"])}
    validator = validate_local_path_timing if timing else validate_local_path_identity
    assert validator(value) == validator(expected)
    moved = tmp_path / "relocated"
    shutil.copytree(path.parent, moved)
    assert list(open_local_paths(moved / path.name, header, verify_digest=True)["paths"]) == expected["paths"]
    summary = tmp_path / "summary.json"
    with mock.patch.object(LocalPathRows, "__iter__", side_effect=AssertionError("summary enumerated paths")):
        write_json(summary, {"schema": "emuflow.phase7b-physical-summary/v1", "local_path_timing": {"F0": value}})
    assert summary.stat().st_size < 4096
    assert list(read_json(summary)["local_path_timing"]["F0"]["paths"]) == expected["paths"]


@pytest.mark.parametrize("damage", ["duplicate", "nan", "boolean", "negative-delay", "bad-chain", "unknown-field"])
def test_writer_rejects_bad_rows_without_publishing(tmp_path, damage):
    row = record(timing=True)
    records = [row]
    if damage == "duplicate":
        records *= 2
    elif damage == "nan":
        row["clock_period_ns"] = float("nan")
    elif damage == "boolean":
        row["clock_period_ns"] = True
    elif damage == "negative-delay":
        row["delay_ns"] = -1.0
    elif damage == "bad-chain":
        row["path_pins"] = ["not allowed for fallback"]
    else:
        row["not-owned"] = "extra"
    path = tmp_path / "new.json"
    with pytest.raises(ValidationError):
        write_local_paths(path, metadata(True), iter(records))
    assert not path.exists()
    assert not list(tmp_path.glob("*.sqlite"))


def test_failed_publication_preserves_existing_owner_and_cleans_new_scratch(tmp_path):
    path = tmp_path / "local.json"
    write_local_paths(path, metadata(), [record()])
    before = path.read_bytes()
    files = set(tmp_path.iterdir())
    with mock.patch("emuflow.io.write_json", side_effect=OSError("quota exceeded")):
        with pytest.raises(OSError, match="quota"):
            write_local_paths(path, metadata(), [record(1)])
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == files
    assert read_json(path)["paths"][0] == record()


@pytest.mark.parametrize("damage", ["truncate", "mixed", "metadata", "count", "row-delay", "pins"])
def test_corrupt_and_mixed_inputs_fail(tmp_path, damage):
    path = tmp_path / "local.json"
    write_local_paths(path, metadata(True), [record(timing=True)])
    header = json.loads(path.read_text())
    payload = path.parent / header["storage"]["path"]
    if damage == "truncate":
        payload.write_bytes(payload.read_bytes()[:128])
    elif damage == "metadata":
        header["metadata"]["fpga"] = "other"
    elif damage == "count":
        header["storage"]["records"] = 99
    elif damage == "mixed":
        other = tmp_path / "other.json"
        write_local_paths(other, {**metadata(True), "fpga": "F1"}, [{**record(timing=True), "fpga": "F1"}])
        header["storage"] = json.loads(other.read_text())["storage"]
    elif damage == "row-delay":
        with sqlite3.connect(payload) as database:
            database.execute("UPDATE paths SET delay=-1")
    else:
        with sqlite3.connect(payload) as database:
            database.execute("INSERT INTO path_pins VALUES ('p00000000',0,'bad')")
    with pytest.raises(ValidationError):
        validate_local_path_timing(open_local_paths(path, header))


def test_streaming_population_does_not_accumulate_dicts(tmp_path):
    path = tmp_path / "many.json"
    tracemalloc.start()
    try:
        write_local_paths(path, metadata(True), (record(i, timing=True) for i in range(20000)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024 * 1024
    rows = read_json(path)["paths"]
    assert len(rows) == 20000
    assert rows[-1]["id"] == "p00019999"
    assert path.stat().st_size < 4096
    with mock.patch.object(LocalPathRows, "__iter__", side_effect=AssertionError("validator rebuilt all rows")):
        assert validate_local_path_timing(read_json(path))["local_paths"] == 20000


def test_prepared_paths_keep_only_typed_sql_rows(tmp_path):
    store = PreparedPathStore(tmp_path)
    ids = ["z", "a", '引号"']
    store.add_ids(ids)
    assert store.sorted_id_digest() == path_id_set_sha256(ids)
    endpoint = {"object": "x/Q", "instance": "x", "port": "Q", "bit": 0}
    value = {"id": "a", "clock_domain": "clk", "clock_period_ns": 10.0,
             "slack_ns": 1.0, "fixed_delay_ns": 9.0, "startpoint": endpoint, "endpoint": endpoint}
    store.add("F0", value)
    prepared = store.finish()
    assert prepared == {"F0": (value,)}


def test_global_sta_binding_preserves_every_event_and_metadata(tmp_path, monkeypatch):
    from emuflow.global_sta import bind_physical_checks
    import emuflow.system_timing as system
    import emuflow.tdm as tdm
    monkeypatch.setattr(system, "_endpoint_delay_database", lambda _: {})
    monkeypatch.setattr(system, "_logic_segment_database", lambda _: {})
    monkeypatch.setattr(system, "_board_link_delay_database", lambda *_: {})
    monkeypatch.setattr(tdm, "is_fixed_slot_transport_schedule", lambda _: True)
    monkeypatch.setattr(tdm, "reconstruct_tdm_schedule_timing_paths", lambda *_: [])
    rows = [record(i, timing=True) for i in range(3)]
    meta = metadata(True)
    meta["source"].update(original_paths=3, original_path_ids_sha256=path_id_set_sha256([r["id"] for r in rows]))
    path = tmp_path / "timing.json"
    write_local_paths(path, meta, iter(rows))
    source = {**meta, "coverage": {"local_paths": 3}, "paths": rows}
    measurements = []
    for database in (source, read_json(path)):
        info = {}
        checks = bind_physical_checks(
            {"virtual_dut_clocks": {"clk": {"nominal_period_ns": 40.0}}},
            {"routes": []}, {"entries": []}, {"local_path_timing": {"F0": database}}, None,
            metadata=info, routes_artifact_sha256="a" * 64,
        )
        measurements.append((checks, info))
    assert measurements[0] == measurements[1]
    assert len(measurements[0][0]) == 6
