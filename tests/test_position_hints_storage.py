import copy
import json
import shutil

import pytest

import emuflow.position_hints_storage as position_storage
from emuflow.errors import ValidationError
from emuflow.position_hints_storage import (
    POSITION_HINTS_STORAGE_SCHEMA,
    PositionHintsView,
    read_position_hints,
    write_position_hints_storage,
)


def _positions():
    return {
        "schema": "emuflow.signal-position-hints/v1",
        "design": "position-store",
        "platform": "board",
        "provider": "openparf-lookahead-centroid-v1",
        "region_count": 3,
        "metrics": {"signals": 2, "endpoint_centroid_fallbacks": 1},
        "entries": [
            {
                "schedule_entry": "entry-b",
                "source_y": 0.8,
                "sink_y": 0.7,
                "source_region": 2,
                "sink_region": 2,
                "source_fallback": False,
                "sink_fallback": False,
            },
            {
                "schedule_entry": "entry-a",
                "source_y": 0.2,
                "sink_y": 0.5,
                "source_region": 0,
                "sink_region": 1,
                "source_fallback": True,
                "sink_fallback": False,
            },
        ],
    }


def test_position_hints_indexed_round_trip_order_and_relocation(tmp_path):
    expected = _positions()
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(expected), encoding="utf-8")
    assert read_position_hints(legacy_path) == expected
    path = tmp_path / "position_hints.json"
    view = write_position_hints_storage(path, expected)

    assert isinstance(view, PositionHintsView)
    assert view.materialize() == expected
    assert [
        item["schedule_entry"] for item in view.iter_by_identity()
    ] == ["entry-a", "entry-b"]
    selector = json.loads(path.read_text(encoding="utf-8"))
    assert selector["schema"] == POSITION_HINTS_STORAGE_SCHEMA
    assert selector["entries"]["records"] == 2
    assert path.stat().st_size < 4096

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    shutil.copy2(path, relocated / path.name)
    shutil.copy2(tmp_path / selector["artifact"], relocated / selector["artifact"])
    assert read_position_hints(
        relocated / path.name, materialize=True
    ) == expected


def test_position_hints_streaming_metrics_and_negative_seals(tmp_path):
    expected = _positions()
    header = {key: value for key, value in expected.items() if key != "entries"}
    header.pop("metrics")
    iterations = 0

    def records():
        nonlocal iterations
        iterations += 1
        if iterations > 1:
            raise AssertionError("position records were consumed twice")
        yield from expected["entries"]

    view = write_position_hints_storage(
        tmp_path / "derived.json",
        {**header, "entries": records()},
        derive_metrics=True,
    )
    assert iterations == 1
    assert view.materialize() == expected

    duplicate = copy.deepcopy(expected)
    duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0]))
    duplicate["metrics"]["signals"] += 1
    with pytest.raises(ValidationError, match="repeat"):
        write_position_hints_storage(tmp_path / "duplicate.json", duplicate)

    selector_path = tmp_path / "selector-tampered.json"
    write_position_hints_storage(selector_path, expected)
    selector = json.loads(selector_path.read_text(encoding="utf-8"))
    selector["header"]["design"] = "different"
    selector_path.write_text(json.dumps(selector), encoding="utf-8")
    with pytest.raises(ValidationError, match="logical seal"):
        read_position_hints(selector_path)

    path = tmp_path / "tampered.json"
    write_position_hints_storage(path, expected)
    selector = json.loads(path.read_text(encoding="utf-8"))
    store = tmp_path / selector["artifact"]
    with store.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValidationError, match="seal"):
        read_position_hints(path)


def test_position_hints_selector_failure_preserves_previous_generation(
    tmp_path, monkeypatch
):
    path = tmp_path / "position_hints.json"
    expected = _positions()
    original = write_position_hints_storage(path, expected)
    original_artifact = original.store_path
    original_selector = path.read_bytes()

    def fail_selector(*_args, **_kwargs):
        raise OSError("injected selector failure")

    monkeypatch.setattr(position_storage, "write_json", fail_selector)
    updated = copy.deepcopy(expected)
    updated["design"] = "updated"
    with pytest.raises(OSError, match="injected"):
        write_position_hints_storage(path, updated)

    assert path.read_bytes() == original_selector
    assert original_artifact.is_file()
    assert read_position_hints(path, materialize=True) == expected
    assert list(tmp_path.glob("position_hints.*.sqlite")) == [
        original_artifact
    ]


def test_position_hints_manifest_record_seal_is_bound_to_store(tmp_path):
    path = tmp_path / "position_hints.json"
    write_position_hints_storage(path, _positions())
    selector = json.loads(path.read_text(encoding="utf-8"))
    selector["entries"]["sha256"] = "0" * 64
    selector["logical_sha256"] = position_storage._logical_sha256(
        selector["header"],
        selector["entries"]["records"],
        selector["entries"]["sha256"],
    )
    path.write_text(json.dumps(selector), encoding="utf-8")
    with pytest.raises(ValidationError, match="metadata disagrees"):
        read_position_hints(path)
