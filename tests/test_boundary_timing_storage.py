from __future__ import annotations

import json
import sqlite3
import hashlib
from pathlib import Path

import pytest

from emuflow.boundary_timing_storage import (
    BOUNDARY_TIMING_SCHEMA,
    BOUNDARY_TIMING_STORAGE_SCHEMA,
    BoundaryTimingView,
    publish_boundary_timing_selector,
    read_boundary_timing_storage,
    write_boundary_timing_storage,
)
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


@pytest.mark.parametrize('scope',('partition-boundary','native-word-registered-interface'))
def test_typed_boundary_timing_round_trip_and_relocation(tmp_path: Path,scope) -> None:
    selector = tmp_path / "source" / "boundary-timing.json"
    records = [_record("tx-0", "tx", 0), _record("rx-1", "rx", 1)]
    header=_header();header['measurement_scope']=scope
    if scope=='native-word-registered-interface':
        records[0]['measurement']='logical-source-to-native-tx-word-sampling-pin'
        records[1]['measurement']='registered-native-rx-word-to-shadow-capture-within-fixed-arc'
    with write_boundary_timing_storage(
        selector, header, reversed(records)
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
        write_boundary_timing_storage(
            tmp_path / "boundary-timing.json", _header(), [first, second]
        )


@pytest.mark.parametrize("version", [1, 2])
def test_existing_data_only_boundary_stores_are_readable(tmp_path, version):
    import emuflow.boundary_timing_storage as storage
    current = tmp_path/"current.json"
    rows = [_record("rx-0", "rx", 0), _record("tx-1", "tx", 1)]
    with write_boundary_timing_storage(current, _header(), rows) as view:
        selector = dict(view.selector)
        old = tmp_path/"old.sqlite"
        with sqlite3.connect(old) as db:
            db.executescript(storage._DDL_V1 if version == 1 else storage._DDL_V2)
            db.execute(f"PRAGMA application_id={storage.BOUNDARY_TIMING_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={version}")
            db.execute("ATTACH DATABASE ? AS current", (str(view.store_path),))
            columns = ",".join(r[1] for r in db.execute("PRAGMA table_info(timings)"))
            db.execute(f"INSERT INTO timings SELECT {columns} FROM current.timings")
            db.execute("INSERT INTO metadata SELECT * FROM current.metadata")
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        owner = tmp_path/"old.json";artifact = owner.with_name(storage._store_name(owner, digest))
        old.rename(artifact)
        selector.update(schema=f"emuflow.boundary-timing-storage/v{version}", user_version=version,
                        artifact=artifact.name, store_bytes=artifact.stat().st_size, store_sha256=digest)
        owner.write_text(json.dumps(selector))
    with read_boundary_timing_storage(owner) as view:
        assert list(view["endpoints"]) == rows


def test_boundary_sampling_cost_requires_every_implemented_endpoint(tmp_path):
    from emuflow.sampling_cost import SAMPLING_COST_POLICY
    with pytest.raises(ValidationError):
        write_boundary_timing_storage(tmp_path/"bad.json",
            {**_header(), "sampling_cost_policy": SAMPLING_COST_POLICY},
            [{**_record("rx-0", "rx", 0), "sampling_cost_ns": 1.}, _record("tx-1", "tx", 1)])


def test_typed_boundary_timing_rejects_store_tampering(tmp_path: Path) -> None:
    selector = tmp_path / "boundary-timing.json"
    view = write_boundary_timing_storage(
        selector, _header(), [_record("tx-0", "tx", 0)]
    )
    store = view.store_path
    view.close()
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal disagrees"):
        read_boundary_timing_storage(selector)


def test_legacy_boundary_timing_requires_explicit_compatibility(
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
    with pytest.raises(ValidationError, match="explicit compatibility"):
        read_boundary_timing_storage(path)
    assert read_boundary_timing_storage(path, allow_legacy=True)["endpoints"] == []
