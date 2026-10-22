from __future__ import annotations

import json
from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.local_path_timing import path_id_set_sha256, validate_local_path_timing
from emuflow.local_path_timing_storage import (
    LOCAL_PATH_TIMING_SCHEMA,
    LOCAL_PATH_TIMING_STORAGE_SCHEMA,
    LocalPathTimingView,
    publish_local_path_timing_selector,
    read_local_path_timing_storage,
    write_local_path_timing_storage,
)
from emuflow.physical_summary_storage import LazyPhysicalEvidenceMap


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


def test_local_path_typed_rejects_duplicate_identity(tmp_path: Path) -> None:
    record = _record("a", 1.0)
    with pytest.raises(ValidationError, match="duplicated"):
        write_local_path_timing_storage(
            tmp_path / "local.json", _header(), [record, record]
        )


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
