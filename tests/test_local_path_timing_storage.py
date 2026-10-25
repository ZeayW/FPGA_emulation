from __future__ import annotations

import json
from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.local_path_timing import (
    LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    path_id_set_sha256,
    read_local_path_identity,
    validate_local_path_identity,
    validate_local_path_timing,
)
from emuflow.local_path_timing_storage import (
    LOCAL_PATH_TIMING_SCHEMA,
    LOCAL_PATH_TIMING_STORAGE_SCHEMA,
    LocalPathTimingView,
    publish_local_path_timing_selector,
    read_local_path_timing_storage,
    write_local_path_timing_storage,
)
from emuflow.physical_summary_storage import LazyPhysicalEvidenceMap
from emuflow.timing_disposition import OPTIMIZED_AWAY


def _header() -> dict[str, object]:
    return {
        "schema": LOCAL_PATH_TIMING_SCHEMA,
        "status": "pass",
        "design": "dut",
        "fpga": "fpga0",
        "provider": "test",
        "qualification": "endpoint-exact",
        "identity_schema": "emuflow.local-path-identity/v2",
        "source": {
            "path_database_sha256": "a" * 64,
            "original_ir_sha256": "b" * 64,
            "assignment_sha256": "c" * 64,
            "routes_sha256": "d" * 64,
            "original_paths": 2,
            "original_path_ids_sha256": path_id_set_sha256(["a", "b"]),
        },
        "coverage": {"local_paths": 2},
    }


def _record(identity: str, delay: float) -> dict[str, object]:
    return {
        "id": identity,
        "kind": "local",
        "fpga": "fpga0",
        "clock_domain": "clk",
        "clock_period_ns": 10.0,
        "required_time_ns": 9.0,
        "start_pin": f"{identity}/Q",
        "end_pin": f"{identity}/D",
        "measurement": "explicit-routed-path-chain",
        "path_pins": [f"{identity}/Q", f"{identity}/D"],
        "delay_ns": delay,
    }


def test_local_path_typed_round_trip_and_relocation(tmp_path: Path) -> None:
    selector = tmp_path / "source" / "local-path-timing.json"
    records = [_record("a", 1.0), _record("b", 2.0)]
    with write_local_path_timing_storage(
        selector, _header(), reversed(records)
    ) as view:
        assert isinstance(view, LocalPathTimingView)
        assert [item["id"] for item in view["paths"]] == ["a", "b"]
        assert validate_local_path_timing(view)["maximum_delay_ns"] == 2.0
    raw = json.loads(selector.read_text(encoding="utf-8"))
    assert raw["schema"] == LOCAL_PATH_TIMING_STORAGE_SCHEMA
    assert "paths" not in raw

    destination = tmp_path / "archive"
    copied, reference = publish_local_path_timing_selector(
        selector, destination, Path("fpga0/local-path-timing.json")
    )
    assert copied["logical_sha256"] == raw["logical_sha256"]
    with read_local_path_timing_storage(
        destination / reference["path"]
    ) as relocated:
        assert list(relocated["paths"]) == records
    with LazyPhysicalEvidenceMap(
        {"fpga0": reference}, destination, expected_design="dut"
    ) as evidence:
        assert [item["id"] for item in evidence["fpga0"]["paths"]] == [
            "a", "b",
        ]


def test_local_path_identity_is_a_zero_delay_typed_projection(
    tmp_path: Path,
) -> None:
    selector = tmp_path / "local-path-identity.json"
    header = {
        **_header(),
        "provider": "original-timing-pathdb-to-xilinx-endpoint-bound-v1",
        "qualification": LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    }
    records = [_record("a", 0.0), _record("b", 0.0)]
    with write_local_path_timing_storage(selector, header, records):
        pass
    with read_local_path_identity(selector) as identity:
        assert validate_local_path_identity(identity) == {
            "status": "pass", "fpga": "fpga0", "local_paths": 2,
        }
        assert identity["schema"] == "emuflow.local-path-identity/v2"
        assert [dict(item) for item in identity["paths"]] == [
            {key: value for key, value in record.items() if key != "delay_ns"}
            for record in records
        ]

    timing_selector = tmp_path / "ordinary-timing.json"
    with write_local_path_timing_storage(
        timing_selector, _header(), records
    ):
        pass
    with pytest.raises(ValidationError, match="identity owner is invalid"):
        read_local_path_identity(timing_selector)


def test_local_path_typed_rejects_duplicate_identity(tmp_path: Path) -> None:
    record = _record("a", 1.0)
    with pytest.raises(ValidationError, match="duplicated"):
        write_local_path_timing_storage(
            tmp_path / "local.json", _header(), [record, record]
        )


def test_eliminated_local_member_round_trip_and_raw_validation(tmp_path: Path) -> None:
    measured = _record("a", 1.0)
    eliminated = {**_record("b", 2.0), "measurement": OPTIMIZED_AWAY,
                  "path_pins": [], "delay_ns": None, "optimization_source": "s7"}
    selector = tmp_path / "timing.json"
    with write_local_path_timing_storage(selector, _header(), [measured, eliminated]) as view:
        assert list(view["paths"]) == [measured, eliminated]
        checked = validate_local_path_timing(view)
        assert checked["local_paths"] == 2
        assert checked["implemented_paths"] == 1
        assert checked["optimized_away_paths"] == 1
        assert checked["maximum_delay_ns"] == 1.0
        raw = dict(view)
        raw["paths"] = list(view["paths"])
        assert validate_local_path_timing(raw) == checked
    _, ref = publish_local_path_timing_selector(selector, tmp_path / "published", Path("fpga0/local.json"))
    with read_local_path_timing_storage(tmp_path / "published" / ref["path"]) as view:
        assert list(view["paths"])[1]["delay_ns"] is None


@pytest.mark.parametrize("mutation", [
    {"delay_ns": 0.0}, {"delay_ns": False}, {"optimization_source": "unknown"},
    {"optimization_source": None}, {"actual_start_object": "invented/Q"},
    {"measurement": "endpoint-longest-path-fallback", "delay_ns": 0.0},
])
def test_eliminated_local_member_rejects_fake_measurements(tmp_path: Path, mutation) -> None:
    eliminated = {**_record("b", 2.0), "measurement": OPTIMIZED_AWAY,
                  "path_pins": [], "delay_ns": None, "optimization_source": "s7", **mutation}
    with pytest.raises(ValidationError):
        write_local_path_timing_storage(tmp_path / "timing.json", _header(), [_record("a", 1.0), eliminated])


def test_reads_historical_v1_typed_generation(tmp_path: Path) -> None:
    import sqlite3
    from emuflow.io import file_sha256
    from emuflow.local_path_timing_storage import _DDL_V1, _store_name, LOCAL_PATH_TIMING_APPLICATION_ID
    selector = tmp_path / "current.json"
    with write_local_path_timing_storage(selector, _header(), [_record("a", 1.0), _record("b", 2.0)]) as view:
        current = view.store_path
        raw = dict(view.selector)
    temporary = tmp_path / "historical.sqlite"
    db = sqlite3.connect(temporary, uri=True)
    db.executescript(_DDL_V1)
    db.execute(f"PRAGMA application_id={LOCAL_PATH_TIMING_APPLICATION_ID}")
    db.execute("PRAGMA user_version=1")
    db.execute("ATTACH DATABASE ? AS newer", (f"file:{current}?mode=ro&immutable=1",))
    db.execute("INSERT INTO timings SELECT seq,id,kind,fpga,clock_domain,clock_period_ns,required_time_ns,start_pin,end_pin,measurement,delay_ns FROM newer.timings")
    for table in ("metadata", "path_pins"):
        db.execute(f"INSERT INTO {table} SELECT * FROM newer.{table}")
    db.commit()
    db.close()
    digest = file_sha256(temporary)
    historical = tmp_path / "historical.json"
    target = historical.with_name(_store_name(historical, digest))
    temporary.rename(target)
    raw.update(schema="emuflow.local-path-timing-storage/v1", user_version=1,
               artifact=target.name, store_sha256=digest, store_bytes=target.stat().st_size)
    historical.write_text(json.dumps(raw))
    with read_local_path_timing_storage(historical) as view:
        assert list(view["paths"]) == [_record("a", 1.0), _record("b", 2.0)]
        assert view.validate_timing()["optimized_away_paths"] == 0


def test_local_path_typed_rejects_incomplete_coverage(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="header counts disagree"):
        write_local_path_timing_storage(
            tmp_path / "local.json", _header(), [_record("a", 1.0)]
        )


def test_local_path_typed_rejects_store_tampering(tmp_path: Path) -> None:
    selector = tmp_path / "local.json"
    view = write_local_path_timing_storage(
        selector, _header(), [_record("a", 1.0), _record("b", 2.0)]
    )
    store = view.store_path
    view.close()
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal disagrees"):
        read_local_path_timing_storage(selector)


def test_local_path_legacy_json_requires_explicit_compatibility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.json"
    value = {**_header(), "paths": [_record("a", 1.0), _record("b", 2.0)]}
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValidationError, match="explicit compatibility"):
        read_local_path_timing_storage(path)
    assert len(read_local_path_timing_storage(path, allow_legacy=True)["paths"]) == 2
