import hashlib
import json
import sqlite3

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.logic_segment_identity_storage import write_logic_segment_identity_storage
from emuflow.logic_segment_timing import (
    VIVADO_LOGIC_SAMPLING_COST_HEADER, import_vivado_logic_segment_timing,
)
import emuflow.logic_segment_timing_storage as storage
from emuflow.sampling_cost import SAMPLING_COST_POLICY, validate_sampling_cost


def identity(tmp_path):
    header = dict(schema="emuflow.logic-segment-identity/v1", status="pass",
                  design="dut", platform="board", fpga="fpga0", provider="test",
                  coverage={}, system_routes_sha256="a" * 64,
                  tdm_schedule_sha256="b" * 64)
    rows = [dict(id=f"s{i}", kind="launch", system_path=f"p{i}", member_path=f"m{i}",
                 cut_index=0, fpga="fpga0", replace_tx_endpoint=f"tx{i}",
                 start_pin=f"launch{i}/Q", end_pin=f"sink{i}/D") for i in range(3)]
    path = tmp_path / "identity.json"
    write_logic_segment_identity_storage(path, header=header, segments=rows).close()
    return path, rows


def measurements(path, rows, costs):
    with path.open("w") as stream:
        stream.write(VIVADO_LOGIC_SAMPLING_COST_HEADER + "\n")
        for row, cost in zip(rows, costs):
            stream.write("\t".join([
                row["id"].encode().hex(), row["kind"], "2.25",
                row["start_pin"].encode().hex(), row["end_pin"].encode().hex(),
                "endpoint-exact", row["start_pin"].encode().hex(),
                row["end_pin"].encode().hex(), *cost,
            ]) + "\n")


def test_indexed_import_preserves_signed_cost_once_and_keeps_data_delay(tmp_path):
    source, rows = identity(tmp_path)
    raw, output = tmp_path / "query.tsv", tmp_path / "timing.json"
    measurements(raw, rows, [("1.5", "-0.75"), ("-0.5", "-2.75"), ("0", "-2.25")])
    result = import_vivado_logic_segment_timing(raw, source, output)
    assert result["status"] == "pass"
    with storage.read_logic_segment_timing_storage(output) as view:
        assert view["sampling_cost_policy"] == SAMPLING_COST_POLICY
        assert [r["sampling_cost_ns"] for r in view["segments"]] == [1.5, -0.5, 0.]
        assert [r["delay_ns"] for r in view["segments"]] == [2.25] * 3
        assert view.selector["source"]["sampling_cost_policy"] == SAMPLING_COST_POLICY
        with sqlite3.connect(view.store_path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(segments)")]
            assert "sampling_cost_ns" in columns
            assert "check_clock_adjustment_ns" not in columns
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("damaged", [
    ("1.5", "0"), ("", ""), ("NaN", "NaN"), ("Inf", "Inf"),
    ("1.5", "-0.75", "unexpected"),
])
def test_invalid_cost_does_not_replace_published_measurements(tmp_path, damaged):
    source, rows = identity(tmp_path)
    raw, output = tmp_path / "query.tsv", tmp_path / "timing.json"
    measurements(raw, rows, [("1.5", "-0.75")] * 3)
    import_vivado_logic_segment_timing(raw, source, output)
    before = output.read_bytes()
    measurements(raw, rows, [("1.5", "-0.75"), damaged, ("1.5", "-0.75")])
    with pytest.raises(ValidationError):
        import_vivado_logic_segment_timing(raw, source, output)
    assert output.read_bytes() == before


def test_policy_and_optimized_away_cost_coverage_are_explicit():
    eliminated = dict(measurement="optimized-away", delay_ns=None)
    validate_sampling_cost(eliminated, SAMPLING_COST_POLICY)
    for row, policy in [
        ({"sampling_cost_ns": 0.}, None), ({}, SAMPLING_COST_POLICY),
        ({**eliminated, "sampling_cost_ns": 0.}, SAMPLING_COST_POLICY),
        ({"sampling_cost_ns": True}, SAMPLING_COST_POLICY),
        ({"sampling_cost_ns": float("nan")}, SAMPLING_COST_POLICY),
        ({"sampling_cost_ns": 10 ** 1000}, SAMPLING_COST_POLICY),
    ]:
        with pytest.raises(ValidationError):
            validate_sampling_cost(row, policy)


def test_original_local_path_import_retains_cost_and_data(tmp_path):
    from tests.test_local_path_timing_storage import _header, _record
    from emuflow.vivado_physical_timing import import_vivado_local_timing
    from emuflow.local_path_timing_storage import read_local_path_timing_storage
    header = {key: value for key, value in _header().items()
              if key in {"status", "design", "fpga", "source", "coverage"}}
    header["schema"] = "emuflow.local-path-identity/v2"
    rows = [{**_record(name, 0.), "measurement": "endpoint-longest-path-fallback", "path_pins": []}
            for name in ["a", "b"]]
    identity_path = tmp_path/"local-identity.sqlite"
    with sqlite3.connect(identity_path) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("CREATE TABLE paths(id TEXT PRIMARY KEY,record_json TEXT)")
        db.execute("INSERT INTO metadata VALUES ('header',?)", (json.dumps(header),))
        db.executemany("INSERT INTO paths VALUES (?,?)", [
            (row["id"], json.dumps({key: value for key, value in row.items() if key != "delay_ns"}))
            for row in rows])
    raw, output = tmp_path/"local-costs.tsv", tmp_path/"local-timing.json"
    measurements(raw, rows, [("1.5", "-0.75"), ("-0.5", "-2.75")])
    assert import_vivado_local_timing(raw, identity_path, output)["implemented_paths"] == 2
    with read_local_path_timing_storage(output) as view:
        assert view["sampling_cost_policy"] == SAMPLING_COST_POLICY
        assert [row["sampling_cost_ns"] for row in view["paths"]] == [1.5, -0.5]
        assert [row["delay_ns"] for row in view["paths"]] == [2.25, 2.25]


@pytest.mark.parametrize("version", [1, 2])
def test_existing_data_only_stores_remain_readable(tmp_path, version):
    header = dict(schema="emuflow.logic-segment-timing/v1", status="pass", design="dut",
                  platform="board", fpga="fpga0", provider="test", qualification="test",
                  coverage={}, system_routes_sha256="a" * 64, tdm_schedule_sha256="b" * 64)
    _, rows = identity(tmp_path)
    path = tmp_path / "new.json"
    with storage.write_logic_segment_timing_storage(
        path, header=header, segments=({**r, "delay_ns": 2.25} for r in rows)
    ) as current:
        selector = dict(current.selector)
        old = tmp_path / "old.sqlite"
        with sqlite3.connect(old) as db:
            db.executescript(storage._DDL_V1 if version == 1 else storage._DDL_V2)
            db.execute(f"PRAGMA application_id={storage.LOGIC_SEGMENT_TIMING_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={version}")
            db.execute("ATTACH DATABASE ? AS current", (str(current.store_path),))
            db.execute("INSERT INTO metadata SELECT * FROM current.metadata")
            columns = ",".join(row[1] for row in db.execute("PRAGMA table_info(segments)"))
            db.execute(f"INSERT INTO segments SELECT {columns} FROM current.segments")
        selector.update(schema=f"emuflow.logic-segment-timing-storage/v{version}", user_version=version)
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        owner = tmp_path / "old.json"
        artifact = owner.with_name(owner.name + ".store." + digest + ".sqlite")
        old.rename(artifact)
        selector.update(artifact=artifact.name, store_bytes=artifact.stat().st_size, store_sha256=digest)
        write_json(owner, selector)
    with storage.read_logic_segment_timing_storage(owner) as view:
        assert [r["delay_ns"] for r in view["segments"]] == [2.25] * 3
        assert all("sampling_cost_ns" not in row for row in view["segments"])
