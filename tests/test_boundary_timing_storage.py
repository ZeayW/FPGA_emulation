from __future__ import annotations

import json
from pathlib import Path

import pytest

from emuflow.boundary_timing_storage import (BOUNDARY_TIMING_SCHEMA, BOUNDARY_TIMING_STORAGE_SCHEMA, BoundaryTimingView, publish_boundary_timing_selector, read_boundary_timing_storage)
from tests.timing_facts import boundary_timing_facts
from emuflow.errors import ValidationError
from emuflow.physical_summary_storage import LazyPhysicalEvidenceMap


def _header() -> dict[str, object]:
    return {
        "schema": BOUNDARY_TIMING_SCHEMA,
        "status": "pass",
        "design": "dut",
        "platform": "board",
        "fpga": "fpga0",
        "provider": "test",
        "qualification": "endpoint-exact",
        "measurement_scope": "partition-boundary",
    }


def _record(endpoint_id: str, kind: str, index: int) -> dict[str, object]:
    return {
        "id": endpoint_id,
        "kind": kind,
        "schedule_entry": f"schedule-{index}",
        "delay_ns": float(index) / 10.0,
        "start_object": f"start-{index}",
        "end_object": f"end-{index}",
        "measurement": (
            "logical-source-to-tx-port"
            if kind == "tx"
            else "rx-port-to-shadow-capture"
        ),
    }


def test_typed_boundary_timing_round_trip_and_relocation(tmp_path: Path) -> None:
    selector = tmp_path / "source" / "boundary-timing.json"
    records = [_record("tx-0", "tx", 0), _record("rx-1", "rx", 1)]
    with boundary_timing_facts(
        selector, _header(), reversed(records)
    ) as view:
        assert isinstance(view, BoundaryTimingView)
        assert [item["id"] for item in view["endpoints"]] == ["rx-1", "tx-0"]
        assert view["coverage"] == {"endpoints": 2, "tx": 1, "rx": 1}
    raw = json.loads(selector.read_text(encoding="utf-8"))
    assert raw["schema"] == BOUNDARY_TIMING_STORAGE_SCHEMA
    assert "endpoints" not in raw
    assert raw["store_bytes"] > 0

    destination = tmp_path / "archive"
    copied, reference = publish_boundary_timing_selector(
        selector, destination, Path("fpga0/boundary-timing.json")
    )
    assert copied["logical_sha256"] == raw["logical_sha256"]
    assert reference["logical_schema"] == BOUNDARY_TIMING_SCHEMA
    with read_boundary_timing_storage(
        destination / reference["path"]
    ) as relocated:
        assert list(relocated["endpoints"]) == [records[1], records[0]]
    with LazyPhysicalEvidenceMap(
        {"fpga0": reference},
        destination,
        expected_design="dut",
        expected_platform="board",
    ) as evidence:
        assert [item["id"] for item in evidence["fpga0"]["endpoints"]] == [
            "rx-1", "tx-0",
        ]


def test_typed_boundary_timing_rejects_duplicate_schedule_kind(
    tmp_path: Path,
) -> None:
    first = _record("tx-0", "tx", 0)
    second = _record("tx-1", "tx", 1)
    second["schedule_entry"] = first["schedule_entry"]
    with pytest.raises(ValidationError, match="duplicated"):
        boundary_timing_facts(
            tmp_path / "boundary-timing.json", _header(), [first, second]
        )


def test_typed_boundary_timing_rejects_store_tampering(tmp_path: Path) -> None:
    selector = tmp_path / "boundary-timing.json"
    view = boundary_timing_facts(
        selector, _header(), [_record("tx-0", "tx", 0)]
    )
    store = view.store_path
    view.close()
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal disagrees"):
        read_boundary_timing_storage(selector)


def test_old_boundary_timing_json_is_retired(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({
            **_header(),
            "coverage": {"endpoints": 0, "tx": 0, "rx": 0},
            "endpoints": [],
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="selector.*malformed|storage schema"):
        read_boundary_timing_storage(path)
    with pytest.raises(TypeError, match="allow_legacy"):
        read_boundary_timing_storage(path, allow_legacy=True)
