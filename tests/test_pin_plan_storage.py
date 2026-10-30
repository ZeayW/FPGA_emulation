import copy
import json
import shutil
from unittest import mock

import pytest

import tests.pin_plan_storage_fixture as pin_storage
from emuflow.common.errors import ValidationError
from emuflow_tools.transport.pin_plan_storage import PIN_PLAN_STORAGE_SCHEMA, PinPlanEntrySequence, PinPlanView, read_pin_plan
from tests.pin_plan_storage_fixture import write_pin_plan_storage
from emuflow_tools.transport.pin_planning import CHIMEW_PIN_PLAN_PROVIDER, PIN_PLAN_SCHEMA


def _plan():
    return {
        "schema": PIN_PLAN_SCHEMA,
        "design": "indexed-pin-plan",
        "platform": "test-platform",
        "provider": CHIMEW_PIN_PLAN_PROVIDER,
        "configuration": {"refinement_iterations": 4},
        "weights": {"crossing": 1.0, "position": 2.0},
        "metrics": {"signals": 2, "groups": 2},
        # Preserve compatibility array order independently from the indexed
        # lane cursor's identity order.
        "entries": [
            {
                "schedule_entry": "entry-b",
                "group": 9,
                "physical_lane": 3,
                "logical_lane": 1,
            },
            {
                "schedule_entry": "entry-a",
                "group": 7,
                "physical_lane": 2,
                "logical_lane": 0,
            },
        ],
    }


def test_indexed_pin_plan_round_trip_and_relocation(tmp_path):
    expected = _plan()
    path = tmp_path / "pin_plan.json"
    view = write_pin_plan_storage(path, expected)

    assert isinstance(view, PinPlanView)
    assert view.materialize() == expected
    assert list(view.iter_lane_records()) == [
        ("entry-a", 2, 0),
        ("entry-b", 3, 1),
    ]
    with mock.patch.object(
        PinPlanEntrySequence,
        "__iter__",
        side_effect=AssertionError("typed lane cursor scanned record JSON"),
    ):
        assert list(view.iter_lane_records()) == [
            ("entry-a", 2, 0),
            ("entry-b", 3, 1),
        ]
    selector = json.loads(path.read_text(encoding="utf-8"))
    assert selector["schema"] == PIN_PLAN_STORAGE_SCHEMA
    assert selector["entries"]["records"] == 2
    assert path.stat().st_size < 4096

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    shutil.copy2(path, relocated / path.name)
    shutil.copy2(tmp_path / selector["artifact"], relocated / selector["artifact"])
    assert read_pin_plan(relocated / path.name, materialize=True) == expected


def test_typed_lane_cursor_preserves_numeric_schedule_order_past_six_digits(tmp_path):
    plan = _plan()
    plan["entries"] = [
        {
            "schedule_entry": "s1000000",
            "group": 9,
            "physical_lane": 3,
            "logical_lane": 1,
        },
        {
            "schedule_entry": "s999999",
            "group": 7,
            "physical_lane": 2,
            "logical_lane": 0,
        },
    ]
    view = write_pin_plan_storage(tmp_path / "pin_plan.json", plan)
    assert list(view.iter_lane_records()) == [
        ("s999999", 2, 0),
        ("s1000000", 3, 1),
    ]


def test_indexed_pin_plan_rejects_duplicate_or_tampered_records(tmp_path):
    duplicate = _plan()
    duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0]))
    with pytest.raises(ValidationError, match="repeats"):
        write_pin_plan_storage(tmp_path / "duplicate.json", duplicate)

    path = tmp_path / "pin_plan.json"
    write_pin_plan_storage(path, _plan())
    selector = json.loads(path.read_text(encoding="utf-8"))
    store = tmp_path / selector["artifact"]
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal"):
        read_pin_plan(path)


def test_pin_plan_selector_failure_preserves_previous_generation(
    tmp_path, monkeypatch
):
    path = tmp_path / "pin_plan.json"
    expected = _plan()
    original = write_pin_plan_storage(path, expected)
    original_artifact = original.store_path
    original_selector = path.read_bytes()

    def fail_selector(*_args, **_kwargs):
        raise OSError("injected selector failure")

    monkeypatch.setattr("emuflow.storage.store_publication.write_json", fail_selector)
    updated = copy.deepcopy(expected)
    updated["design"] = "updated"
    with pytest.raises(OSError, match="injected"):
        write_pin_plan_storage(path, updated)

    assert path.read_bytes() == original_selector
    assert original_artifact.is_file()
    assert read_pin_plan(path, materialize=True) == expected
    assert list(tmp_path.glob("pin_plan.*.sqlite")) == [original_artifact]


def test_pin_plan_manifest_record_seal_is_bound_to_store(tmp_path):
    path = tmp_path / "pin_plan.json"
    write_pin_plan_storage(path, _plan())
    selector = json.loads(path.read_text(encoding="utf-8"))
    selector["entries"]["sha256"] = "0" * 64
    selector["logical_sha256"] = pin_storage._logical_sha256(
        selector["header"],
        selector["entries"]["records"],
        selector["entries"]["sha256"],
    )
    path.write_text(json.dumps(selector), encoding="utf-8")
    with pytest.raises(ValidationError, match="record seal differs"):
        read_pin_plan(path)
