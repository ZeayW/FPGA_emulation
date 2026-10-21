from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3

import pytest

import emuflow.tdm_schedule_storage as tdm_schedule_storage
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.tdm_schedule_storage import (
    LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_APPLICATION_ID,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TYPED_TDM_SCHEDULE_USER_VERSION,
    TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    TYPED_ENTRY_RELATION_SCHEMA,
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


def _typed_exact_schedule(count: int = 5) -> dict:
    value = _schedule(count)
    value["provider"] = "deterministic-dependency-earliest-slot-v1"
    for index, entry in enumerate(value["entries"]):
        entry.update(
            {
                "id": f"s{index:06d}",
                "hop": 0,
                "capacity_key": "ab:a:b",
                "ready_slot": index,
            }
        )
    return value


def _reseal_typed_store(path, mutate) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    store = path.with_name(manifest["artifact"])
    with sqlite3.connect(store) as connection:
        mutate(connection)
    digest = hashlib.sha256(store.read_bytes()).hexdigest()
    resealed = path.with_name(f"schedule.{digest}.sqlite")
    if resealed != store:
        store.rename(resealed)
    manifest["artifact"] = resealed.name
    manifest["store_bytes"] = resealed.stat().st_size
    manifest["store_sha256"] = digest
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_v3_exact_entries_live_once_in_producer_owned_typed_relation(tmp_path):
    expected = _typed_exact_schedule(17)
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, expected)

    assert manifest["schema"] == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
    assert manifest["entry_relation"] == {
        "schema": TYPED_ENTRY_RELATION_SCHEMA,
        "records": 17,
    }
    assert manifest["artifact"] == (
        f"schedule.{manifest['store_sha256']}.sqlite"
    )
    store = tmp_path / manifest["artifact"]
    with sqlite3.connect(store) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            TYPED_TDM_SCHEDULE_USER_VERSION
        )
        assert connection.execute(
            "SELECT count(*) FROM schedule_entries"
        ).fetchone()[0] == 17
        assert connection.execute(
            "SELECT count(*) FROM records WHERE section='entries'"
        ).fetchone()[0] == 0
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='schedule_entries'"
            )
        }
        assert {
            "schedule_entries_seq",
            "schedule_entries_capacity_order",
            "schedule_entries_collision",
        } <= indexes

    with open_tdm_schedule_view(path) as view:
        assert view.materialize() == expected
        view.validate_logical_seals()
        assert canonical_tdm_schedule_sha256(view) == (
            canonical_tdm_schedule_sha256(expected)
        )
        assert pretty_tdm_schedule_sha256(view) == (
            pretty_tdm_schedule_sha256(expected)
        )
        with view.open_validation_index() as index:
            assert index[("d3", "ab", "a", "b")]["id"] == "s000003"
            assert index._db.execute(
                "SELECT count(*) FROM sqlite_temp_master "
                "WHERE name='validation_entries'"
            ).fetchone()[0] == 0


def test_v3_storage_accepts_integer_values_for_semantic_validator(tmp_path):
    expected = _typed_exact_schedule(1)
    expected["entries"][0].update(
        {
            "hop": -1,
            "slot": -2,
            "lane": -3,
            "ready_slot": -4,
            "arrival_slot": -5,
            "tdm_ratio": 0,
        }
    )
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, expected)

    with open_tdm_schedule_view(path) as view:
        assert view.materialize() == expected


@pytest.mark.parametrize("typed", [False, True])
def test_manifest_publication_failure_preserves_old_generation(
    tmp_path, monkeypatch, typed
):
    path = tmp_path / "schedule.json"
    old_schedule = _typed_exact_schedule(2) if typed else _schedule(2)
    old_manifest = write_tdm_schedule_storage(path, old_schedule)
    old_store = tmp_path / old_manifest["artifact"]

    updated = _typed_exact_schedule(3) if typed else _schedule(3)

    def fail_manifest(*_args, **_kwargs):
        raise RuntimeError("injected manifest publication failure")

    monkeypatch.setattr(tdm_schedule_storage, "write_json", fail_manifest)
    with pytest.raises(RuntimeError, match="injected"):
        write_tdm_schedule_storage(path, updated)

    assert json.loads(path.read_text(encoding="utf-8")) == old_manifest
    assert old_store.exists()
    assert read_json(path) == old_schedule
    assert len(list(tmp_path.glob("schedule.*.sqlite"))) == 2


def test_v3_successful_publication_keeps_previous_generation_readable(tmp_path):
    path = tmp_path / "schedule.json"
    old_schedule = _typed_exact_schedule(2)
    old_manifest = write_tdm_schedule_storage(path, old_schedule)
    old_store = tmp_path / old_manifest["artifact"]

    new_schedule = _typed_exact_schedule(3)
    new_manifest = write_tdm_schedule_storage(path, new_schedule)
    assert new_manifest["artifact"] != old_manifest["artifact"]
    assert old_store.exists()

    old_manifest_path = tmp_path / "old-schedule.json"
    old_manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")
    assert read_json(old_manifest_path) == old_schedule
    assert read_json(path) == new_schedule


def test_v3_rejects_non_content_addressed_artifact_name(tmp_path):
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, _typed_exact_schedule(1))
    manifest["artifact"] = "schedule.sqlite"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="artifact identity"):
        open_tdm_schedule_view(path)


def test_v3_open_rejects_noncontiguous_entry_sequence(tmp_path):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(3))
    _reseal_typed_store(
        path,
        lambda connection: connection.execute(
            "UPDATE schedule_entries SET seq=9 WHERE seq=1"
        ),
    )

    with pytest.raises(ValidationError, match="sequence is not contiguous"):
        open_tdm_schedule_view(path)


def test_v3_open_rejects_wrong_required_index_columns(tmp_path):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(3))

    def mutate(connection):
        connection.execute("DROP INDEX schedule_entries_collision")
        connection.execute(
            "CREATE INDEX schedule_entries_collision "
            "ON schedule_entries(capacity_key,lane,slot)"
        )

    _reseal_typed_store(path, mutate)
    with pytest.raises(ValidationError, match="index definition"):
        open_tdm_schedule_view(path)


def test_v3_typed_entry_mutation_breaks_logical_seal_validation(tmp_path):
    expected = _typed_exact_schedule(3)
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, expected)
    store = tmp_path / manifest["artifact"]
    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE schedule_entries SET slot=slot+1 WHERE seq=1"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["store_bytes"] = store.stat().st_size
    manifest["store_sha256"] = hashlib.sha256(store.read_bytes()).hexdigest()
    resealed_store = tmp_path / f"schedule.{manifest['store_sha256']}.sqlite"
    store.rename(resealed_store)
    manifest["artifact"] = resealed_store.name
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with open_tdm_schedule_view(path) as view:
        with pytest.raises(ValidationError, match="logical seal|logical digest"):
            view.validate_logical_seals()


def test_typed_transport_cursor_avoids_python_json_decode(tmp_path, monkeypatch):
    expected = _schedule(3)
    expected["entries"][1]["tdm_ratio"] = 4
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, expected)

    with open_tdm_schedule_view(path) as view:
        def reject_decode(*_args, **_kwargs):
            raise AssertionError("typed transport cursor decoded JSON in Python")

        monkeypatch.setattr(tdm_schedule_storage.json, "loads", reject_decode)
        records = list(view.iter_transport_records())

    assert [record.entry_id for record in records] == ["e0", "e1", "e2"]
    assert records[0]._asdict() == {
        "entry_id": "e0",
        "demand": "d0",
        "net": "n0",
        "link": "ab",
        "source": "a",
        "sink": "b",
        "slot": 0,
        "arrival_slot": 1,
        "lane": 0,
        "tdm_ratio": None,
        "route_source": "a",
    }
    assert records[1].tdm_ratio == 4


def test_typed_transport_cursor_preserves_sequence_across_million_boundary(
    tmp_path,
):
    value = _schedule(2)
    value["entries"][0]["id"] = "s999999"
    value["entries"][1]["id"] = "s1000000"
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, value)

    with open_tdm_schedule_view(path) as view:
        assert [
            record.entry_id for record in view.iter_transport_records()
        ] == ["s999999", "s1000000"]
        assert [
            record.entry_id
            for record in view.iter_transport_records(identity_order=True)
        ] == ["s999999", "s1000000"]


@pytest.mark.parametrize(
    "mutation", ["net", "slot-type", "null-ratio", "missing-route"]
)
def test_typed_transport_cursor_fails_closed(tmp_path, mutation):
    value = _schedule(1)
    if mutation == "net":
        value["entries"][0]["net"] = "different-net"
        message = "malformed"
    elif mutation == "slot-type":
        value["entries"][0]["slot"] = True
        message = "malformed"
    elif mutation == "null-ratio":
        value["entries"][0]["tdm_ratio"] = None
        message = "malformed"
    else:
        value["entries"][0]["demand"] = "missing-demand"
        message = "coverage"
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, value)

    with open_tdm_schedule_view(path) as view:
        with pytest.raises(ValidationError, match=message):
            list(view.iter_transport_records())


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


def test_v3_consumes_native_typed_cursor_in_one_pass(tmp_path):
    class TypedEntryRecords(list):
        canonical_record_cursor_schema = (
            "emuflow.native-exact-schedule-entry-cursor/v1"
        )
        typed_record_cursor_schema = (
            "emuflow.native-exact-schedule-typed-entry-cursor/v1"
        )

        def __init__(self, records):
            super().__init__(records)
            self.cursor_iterations = 0

        def __iter__(self):
            raise AssertionError("typed entries were decoded as objects")

        def iter_canonical_records(self):
            raise AssertionError("parallel canonical cursor was opened")

        def iter_typed_records(self):
            self.cursor_iterations += 1
            for item in list.__iter__(self):
                yield (
                    item["id"],
                    (
                        item["id"],
                        item["demand"],
                        item["net"],
                        item["hop"],
                        item["link"],
                        item["from"],
                        item["to"],
                        item["capacity_key"],
                        item["slot"],
                        item["lane"],
                        item["ready_slot"],
                        item["arrival_slot"],
                        item.get("tdm_ratio"),
                    ),
                )

    expected = _typed_exact_schedule(10_000)
    value = dict(expected)
    entries = TypedEntryRecords(expected["entries"])
    value["entries"] = entries
    manifest = write_tdm_schedule_storage(tmp_path / "schedule.json", value)

    assert entries.cursor_iterations == 1
    assert manifest["schema"] == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
    assert manifest["logical_sha256"] == canonical_tdm_schedule_sha256(
        expected
    )
    assert manifest["logical_pretty_sha256"] == pretty_tdm_schedule_sha256(
        expected
    )
    with sqlite3.connect(tmp_path / manifest["artifact"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_entries"
        ).fetchone()[0] == 10_000
        assert connection.execute(
            "SELECT count(*) FROM records WHERE section='entries'"
        ).fetchone()[0] == 0


def test_v3_rejects_malformed_typed_cursor_record(tmp_path):
    value = _typed_exact_schedule(1)
    entry = value["entries"][0]

    class MismatchedTypedEntries(list):
        typed_record_cursor_schema = (
            "emuflow.native-exact-schedule-typed-entry-cursor/v1"
        )

        def __iter__(self):
            raise AssertionError("typed entries were decoded as objects")

        def iter_typed_records(self):
            yield (
                entry["id"],
                (
                    "s999999",
                    entry["demand"],
                    entry["net"],
                    entry["hop"],
                    entry["link"],
                    entry["from"],
                    entry["to"],
                    entry["capacity_key"],
                    entry["slot"],
                    entry["lane"],
                    entry["ready_slot"],
                    entry["arrival_slot"],
                    None,
                ),
            )

    value["entries"] = MismatchedTypedEntries([entry])
    with pytest.raises(ValidationError, match="typed schedule entry is malformed"):
        write_tdm_schedule_storage(tmp_path / "schedule.json", value)


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


def test_v2_validation_index_remains_a_compatible_fallback(tmp_path):
    expected = _typed_exact_schedule(4)
    expected["provider"] = "reference-provider"
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, expected)
    assert manifest["schema"] == TDM_SCHEDULE_STORAGE_SCHEMA

    with open_tdm_schedule_view(path) as view:
        with view.open_validation_index() as index:
            assert len(index) == 4
            assert index._db.execute(
                "SELECT count(*) FROM sqlite_temp_master "
                "WHERE name='validation_entries'"
            ).fetchone()[0] == 1


@pytest.mark.parametrize("mutation", ["truncate", "replace", "manifest", "row"])
def test_schedule_store_rejects_corruption_and_mixed_artifacts(tmp_path, mutation):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_manifest = write_tdm_schedule_storage(
        first / "schedule.json", _schedule(4)
    )
    second_manifest = write_tdm_schedule_storage(
        second / "schedule.json", _schedule(7)
    )
    store = first / first_manifest["artifact"]
    if mutation == "truncate":
        store.write_bytes(store.read_bytes()[:-19])
    elif mutation == "replace":
        shutil.copyfile(second / second_manifest["artifact"], store)
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


def test_v3_typed_store_is_byte_deterministic(tmp_path):
    expected = _typed_exact_schedule(5_000)
    manifests = []
    for run in range(2):
        root = tmp_path / f"typed-{run}"
        root.mkdir()
        manifests.append(
            write_tdm_schedule_storage(root / "schedule.json", expected)
        )
    assert manifests[0]["logical_sha256"] == manifests[1]["logical_sha256"]
    assert manifests[0]["logical_pretty_sha256"] == (
        manifests[1]["logical_pretty_sha256"]
    )
    assert manifests[0]["store_sha256"] == manifests[1]["store_sha256"]
