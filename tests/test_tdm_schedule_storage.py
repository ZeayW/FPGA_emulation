from __future__ import annotations

import json
import hashlib
import shutil

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.tdm_schedule_storage import (
    LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_APPLICATION_ID,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    canonical_tdm_schedule_sha256,
    open_tdm_schedule_view,
    pretty_tdm_schedule_sha256,
    write_tdm_schedule_storage,
)


def _schedule(count: int = 5) -> dict:
    return {
        "schema": "emuflow.tdm-schedule/v1",
        "design": "schedule-store-test",
        "platform": "board",
        "provider": "test-provider",
        "transport_semantics": "registered-boundary",
        "route_constraints": {"frame_slots": 32},
        "routes": [
            {
                "id": f"d{index}",
                "net": f"n{index}",
                "source": "a",
                "sinks": ["b"],
                "tree_edges": [{"link": "ab", "from": "a", "to": "b"}],
            }
            for index in range(count)
        ],
        "entries": [
            {
                "id": f"e{index}",
                "demand": f"d{index}",
                "net": f"n{index}",
                "link": "ab",
                "from": "a",
                "to": "b",
                "slot": index,
                "arrival_slot": index + 1,
                "lane": 0,
            }
            for index in range(count)
        ],
        "demand_completions": [
            {"demand": f"d{index}", "completion_slot": index + 1}
            for index in range(count)
        ],
        "domain_schedules": [
            {
                "key": "ab:a:b",
                "link": "ab",
                "direction": "a->b",
                "scheduled_bit_hops": count,
            }
        ],
        "metrics": {"scheduled_bit_hops": count},
    }


def test_schedule_store_round_trip_lazy_iteration_and_relocation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    expected = _schedule(11)
    manifest = write_tdm_schedule_storage(source / "schedule.json", expected)
    assert manifest["schema"] == TDM_SCHEDULE_STORAGE_SCHEMA
    assert manifest["application_id"] == TDM_SCHEDULE_APPLICATION_ID
    assert manifest["sections"]["entries"]["records"] == 11
    assert (source / "schedule.json").stat().st_size < 2048

    with open_tdm_schedule_view(source / "schedule.json") as view:
        assert view["design"] == expected["design"]
        assert len(view["entries"]) == 11
        assert view["entries"][3] == expected["entries"][3]
        assert list(view["entries"]) == expected["entries"]
        expected_pretty = json.dumps(
            expected, indent=2, sort_keys=True
        ) + "\n"
        assert pretty_tdm_schedule_sha256(view) == hashlib.sha256(
            expected_pretty.encode("utf-8")
        ).hexdigest()
    assert read_json(source / "schedule.json") == expected

    relocated = tmp_path / "relocated"
    shutil.copytree(source, relocated)
    assert read_json(relocated / "schedule.json") == expected


def test_schedule_store_publishes_large_section_in_one_pass(tmp_path):
    class SinglePassRecords(list):
        def __init__(self, records):
            super().__init__(records)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("large schedule section was decoded twice")
            yield from super().__iter__()

    expected = _schedule(10_000)
    value = dict(expected)
    entries = SinglePassRecords(expected["entries"])
    value["entries"] = entries
    manifest = write_tdm_schedule_storage(tmp_path / "schedule.json", value)
    assert entries.iterations == 1
    assert manifest["logical_sha256"] == canonical_tdm_schedule_sha256(
        expected
    )
    assert manifest["logical_pretty_sha256"] == pretty_tdm_schedule_sha256(
        expected
    )
    with open_tdm_schedule_view(tmp_path / "schedule.json") as view:
        assert list(view["entries"]) == expected["entries"]


def test_schedule_store_consumes_canonical_entry_cursor_without_decode(tmp_path):
    class CanonicalEntryRecords(list):
        canonical_record_cursor_schema = (
            "emuflow.native-exact-schedule-entry-cursor/v1"
        )

        def __init__(self, records):
            super().__init__(records)
            self.cursor_iterations = 0

        def __iter__(self):
            raise AssertionError("canonical entries were decoded as objects")

        def iter_canonical_records(self):
            self.cursor_iterations += 1
            for item in list.__iter__(self):
                yield item["id"], json.dumps(
                    item,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )

    expected = _schedule(10_000)
    for index, entry in enumerate(expected["entries"]):
        entry.update(
            {
                "id": f"s{index:06d}",
                "capacity_key": "ab:a:b",
                "hop": 0,
                "ready_slot": index,
                "tdm_ratio": 1,
            }
        )
    value = dict(expected)
    entries = CanonicalEntryRecords(expected["entries"])
    value["entries"] = entries
    manifest = write_tdm_schedule_storage(tmp_path / "schedule.json", value)
    assert entries.cursor_iterations == 1
    assert manifest["logical_sha256"] == canonical_tdm_schedule_sha256(
        expected
    )
    assert manifest["logical_pretty_sha256"] == pretty_tdm_schedule_sha256(
        expected
    )
    with open_tdm_schedule_view(tmp_path / "schedule.json") as view:
        assert list(view["entries"]) == expected["entries"]


@pytest.mark.parametrize(
    "mutation",
    ["identity", "malformed-json", "noncanonical-order"],
)
def test_canonical_entry_cursor_fails_closed(tmp_path, mutation):
    value = _schedule(1)
    entry = value["entries"][0]
    entry.update(
        {
            "id": "s000000",
            "capacity_key": "ab:a:b",
            "hop": 0,
            "ready_slot": 0,
            "tdm_ratio": 1,
        }
    )
    encoded = json.dumps(
        entry,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    identity = entry["id"]
    if mutation == "identity":
        identity = "s000001"
    elif mutation == "malformed-json":
        encoded = encoded[:-1] + ",}"
    else:
        encoded = json.dumps(
            dict(reversed(list(entry.items()))),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )

    class InvalidCanonicalEntries(list):
        canonical_record_cursor_schema = (
            "emuflow.native-exact-schedule-entry-cursor/v1"
        )

        def __iter__(self):
            raise AssertionError("canonical entries were decoded as objects")

        def iter_canonical_records(self):
            yield identity, encoded

    value["entries"] = InvalidCanonicalEntries([entry])
    with pytest.raises(ValidationError, match="canonical schedule entry"):
        write_tdm_schedule_storage(tmp_path / "schedule.json", value)


def test_v1_schedule_manifest_remains_readable(tmp_path):
    expected = _schedule(3)
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, expected)
    manifest = json.loads(path.read_text())
    manifest["schema"] = LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA
    manifest.pop("logical_pretty_sha256")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert read_json(path) == expected
    with open_tdm_schedule_view(path) as view:
        assert pretty_tdm_schedule_sha256(view) == pretty_tdm_schedule_sha256(
            expected
        )


@pytest.mark.parametrize("mutation", ["truncate", "replace", "manifest", "row"])
def test_schedule_store_rejects_corruption_and_mixed_artifacts(tmp_path, mutation):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write_tdm_schedule_storage(first / "schedule.json", _schedule(4))
    write_tdm_schedule_storage(second / "schedule.json", _schedule(7))
    store = first / "schedule.sqlite"
    if mutation == "truncate":
        store.write_bytes(store.read_bytes()[:-19])
    elif mutation == "replace":
        shutil.copyfile(second / "schedule.sqlite", store)
    elif mutation == "manifest":
        value = json.loads((first / "schedule.json").read_text())
        value["sections"]["entries"]["records"] += 1
        (first / "schedule.json").write_text(json.dumps(value))
    else:
        data = bytearray(store.read_bytes())
        data[len(data) // 2] ^= 0x5A
        store.write_bytes(data)
    with pytest.raises(ValidationError):
        read_json(first / "schedule.json")


def test_schedule_store_rejects_duplicate_section_identity(tmp_path):
    value = _schedule(2)
    value["entries"][1]["id"] = value["entries"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate"):
        write_tdm_schedule_storage(tmp_path / "schedule.json", value)


def test_schedule_store_is_deterministic_for_repeated_structure(tmp_path):
    expected = _schedule(5_000)
    manifests = []
    for run in range(2):
        root = tmp_path / f"run-{run}"
        root.mkdir()
        manifests.append(
            write_tdm_schedule_storage(root / "schedule.json", expected)
        )
        with open_tdm_schedule_view(root / "schedule.json") as view:
            assert len(view["entries"]) == 5_000
            assert sum(1 for _ in view["entries"]) == 5_000
    assert manifests[0]["logical_sha256"] == manifests[1]["logical_sha256"]
    assert manifests[0]["store_sha256"] == manifests[1]["store_sha256"]
