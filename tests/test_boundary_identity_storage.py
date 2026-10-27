from __future__ import annotations

import json
from pathlib import Path

import pytest

from emuflow.boundary_identity_storage import (
    BOUNDARY_IDENTITY_SCHEMA,
    BOUNDARY_IDENTITY_STORAGE_SCHEMA,
    BoundaryIdentityView,
    publish_boundary_identity_selector,
    read_boundary_identity_storage,
    write_boundary_identity_storage,
)
from emuflow.errors import ValidationError
from emuflow.physical_summary_storage import LazyPhysicalEvidenceMap


def _header() -> dict[str, object]:
    return {
        "schema": BOUNDARY_IDENTITY_SCHEMA,
        "status": "pass",
        "design": "dut",
        "platform": "board",
        "fpga": "fpga0",
        "provider": "test",
    }


def _record(endpoint_id: str, kind: str, lane: int) -> dict[str, object]:
    return {
        "id": endpoint_id,
        "kind": kind,
        "schedule_entry": f"schedule-{lane}",
        "demand": f"demand-{lane}",
        "logical_net": f"net-{lane}",
        "link": "link-0",
        "peer": "fpga1",
        "lane": lane,
        "logical_lane": lane + 10,
        "internal_signal": (
            f"net:net-{lane}" if kind == "tx" else f"shadow:demand-{lane}:fpga0"
        ),
        "source_class": "dut-net" if kind == "tx" else "captured-shadow",
        "merged_ir": {
            "external_port": f"{kind}_link_0_fpga1",
            "external_port_bit": lane,
            "external_net": f"wire-{lane}",
            "logical_net": f"net-{lane}",
            "boundary_register_instances": (
                [] if kind == "tx" else [f"__emuflow_transport__/ff-{lane}"]
            ),
        },
    }


def test_typed_boundary_identity_round_trip_and_relocation(tmp_path: Path) -> None:
    selector = tmp_path / "source" / "boundary-identities.json"
    records = [_record("tx-0", "tx", 0), _record("rx-1", "rx", 1)]
    with write_boundary_identity_storage(selector, _header(), reversed(records)) as view:
        assert isinstance(view, BoundaryIdentityView)
        assert list(view["endpoints"]) == list(reversed(records))
        # Logical order is lexical endpoint ID, independent of insertion order.
        assert [item["id"] for item in view["endpoints"]] == ["rx-1", "tx-0"]
        assert view["coverage"] == {
            "endpoints": 2, "tx": 1, "rx": 1, "external_port_nets": 2,
        }
    raw = json.loads(selector.read_text(encoding="utf-8"))
    assert raw["schema"] == BOUNDARY_IDENTITY_STORAGE_SCHEMA
    assert "endpoints" not in raw
    assert raw["store_bytes"] > 0

    destination = tmp_path / "archive"
    copied, reference = publish_boundary_identity_selector(
        selector, destination, Path("fpga0/boundary-identities.json")
    )
    assert copied["logical_sha256"] == raw["logical_sha256"]
    assert reference["logical_schema"] == BOUNDARY_IDENTITY_SCHEMA
    with read_boundary_identity_storage(
        destination / reference["path"]
    ) as relocated:
        assert list(relocated["endpoints"]) == [records[1], records[0]]
    with LazyPhysicalEvidenceMap(
        {"fpga0": reference},
        destination,
        expected_design="dut",
        expected_platform="board",
    ) as evidence:
        assert [
            item["id"] for item in evidence["fpga0"]["endpoints"]
        ] == ["rx-1", "tx-0"]


def test_typed_boundary_identity_rejects_duplicate_schedule_kind(
    tmp_path: Path,
) -> None:
    first = _record("tx-0", "tx", 0)
    second = _record("tx-1", "tx", 1)
    second["schedule_entry"] = first["schedule_entry"]
    with pytest.raises(ValidationError, match="duplicated"):
        write_boundary_identity_storage(
            tmp_path / "boundary-identities.json", _header(), [first, second]
        )


def test_typed_boundary_identity_rejects_store_tampering(tmp_path: Path) -> None:
    selector = tmp_path / "boundary-identities.json"
    view = write_boundary_identity_storage(
        selector, _header(), [_record("tx-0", "tx", 0)]
    )
    store = view.store_path
    view.close()
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal disagrees"):
        read_boundary_identity_storage(selector)


def test_old_boundary_identity_json_is_retired(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({
            **_header(),
            "coverage": {
                "endpoints": 0, "tx": 0, "rx": 0, "external_port_nets": 0,
            },
            "endpoints": [],
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="selector.*malformed|storage schema"):
        read_boundary_identity_storage(path)
    with pytest.raises(TypeError, match="allow_legacy"):
        read_boundary_identity_storage(path, allow_legacy=True)
