from __future__ import annotations
from tests.schedule_store_fixture import write_tdm_schedule_storage

from contextlib import closing
import json
import hashlib
import shutil
import sqlite3

import pytest

import emuflow.scheduling.tdm_schedule_storage as tdm_schedule_storage
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.scheduling.tdm_schedule_storage import (
    TDM_SCHEDULE_USER_VERSION,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_APPLICATION_ID,
    TYPED_ENTRY_RELATION_SCHEMA,
    TYPED_COMPLETION_RELATION_SCHEMA,
    TYPED_ROUTE_RELATION_SCHEMA,
    canonical_tdm_schedule_sha256,
    open_tdm_schedule_view,
    pretty_tdm_schedule_sha256,
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
                "hop": 0,
                "capacity_key": "ab:a:b",
                "ready_slot": index,
                "arrival_slot": index + 1,
                "lane": 0,
            }
            for index in range(count)
        ],
        "demand_completions": [
            {"demand": f"d{index}", "net": f"n{index}", "source_ready_slot": index,
             "completion_slot": index + 1, "sink_arrival_slots": {"b": index + 1}}
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
    value["qualification"] = "dependency-schedule-readiness-pass"
    for index, entry in enumerate(value["entries"]):
        entry.update(
            {
                "id": f"s{index:06d}",
                "hop": 0,
                "capacity_key": "ab:a:b",
                "ready_slot": index,
            }
        )
    value["demand_completions"] = [
        {
            "demand": f"d{index}",
            "net": f"n{index}",
            "source_ready_slot": index,
            "completion_slot": index + 1,
            "sink_arrival_slots": {"b": index + 1},
        }
        for index in range(count)
    ]
    return value


def _typed_multihop_schedule() -> dict:
    value = _typed_exact_schedule(0)
    value["routes"] = [
        {
            "id": "d0",
            "net": "n0",
            "source": "a",
            "sinks": ["c", "d"],
            "tree_edges": [
                {"link": "ab", "from": "a", "to": "b"},
                {"link": "bc", "from": "b", "to": "c"},
                {"link": "bd", "from": "b", "to": "d"},
            ],
        }
    ]
    value["entries"] = [
        {
            "id": f"s{index:06d}",
            "demand": "d0",
            "net": "n0",
            "hop": index,
            "link": link,
            "from": source,
            "to": sink,
            "capacity_key": f"{link}:{source}:{sink}",
            "slot": index,
            "lane": 0,
            "ready_slot": index,
            "arrival_slot": index + 1,
        }
        for index, (link, source, sink) in enumerate(
            (("ab", "a", "b"), ("bc", "b", "c"), ("bd", "b", "d"))
        )
    ]
    value["demand_completions"] = [
        {
            "demand": "d0",
            "net": "n0",
            "source_ready_slot": 0,
            "completion_slot": 3,
            "sink_arrival_slots": {"c": 2, "d": 3},
        }
    ]
    value["domain_schedules"] = [
        {
            "key": "ab:a:b",
            "link": "ab",
            "direction": "a->b",
            "scheduled_bit_hops": 1,
        }
    ]
    value["metrics"] = {"scheduled_bit_hops": 3}
    return value


def _reseal_typed_store(path, mutate) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    store = path.with_name(manifest["artifact"])
    with closing(sqlite3.connect(store)) as connection, connection:
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


def test_v4_exact_relations_live_once_in_producer_owned_typed_tables(tmp_path):
    expected = _typed_exact_schedule(17)
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, expected)

    assert manifest["schema"] == TDM_SCHEDULE_STORAGE_SCHEMA
    assert manifest["entry_relation"] == {
        "schema": TYPED_ENTRY_RELATION_SCHEMA,
        "records": 17,
    }
    assert manifest["route_relation"] == {
        "schema": TYPED_ROUTE_RELATION_SCHEMA,
        "records": 17,
        "sink_records": 17,
        "edge_records": 17,
    }
    assert manifest["completion_relation"] == {
        "schema": TYPED_COMPLETION_RELATION_SCHEMA,
        "records": 17,
        "arrival_records": 17,
    }
    assert manifest["artifact"] == (
        f"schedule.{manifest['store_sha256']}.sqlite"
    )
    store = tmp_path / manifest["artifact"]
    with closing(sqlite3.connect(store)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            TDM_SCHEDULE_USER_VERSION
        )
        assert connection.execute(
            "SELECT count(*) FROM schedule_entries"
        ).fetchone()[0] == 17
        assert connection.execute(
            "SELECT count(*) FROM records WHERE section='entries'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM records WHERE section='routes'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM records WHERE section='demand_completions'"
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
        for name in ('open_validation_index', 'iter_entry_route_records',
                     'iter_transport_records', 'iter_incoming_records'):
            assert not hasattr(view, name)
        assert not hasattr(tdm_schedule_storage, 'ScheduleValidationIndex')
        assert not hasattr(tdm_schedule_storage, 'ScheduleTransportRecord')
    assert read_json(path) == expected


def test_v4_route_completion_cursors_do_not_decode_json(tmp_path, monkeypatch):
    expected = _typed_exact_schedule(8)
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, expected)

    with open_tdm_schedule_view(path) as view:
        def reject_decode(*_args, **_kwargs):
            raise AssertionError("typed route/completion cursor decoded JSON")

        monkeypatch.setattr(tdm_schedule_storage.json, "loads", reject_decode)
        assert list(view["routes"]) == expected["routes"]
        assert list(view["demand_completions"]) == expected[
            "demand_completions"
        ]
        assert [record['source'] for record in view['routes']] == ['a'] * 8


def test_v4_consumer_cursors_cover_multihop_multisink_routes_without_n_plus_one(
    tmp_path, monkeypatch
):
    expected = _typed_multihop_schedule()
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, expected)

    with open_tdm_schedule_view(path) as view:
        real_connect = sqlite3.connect
        executed: list[str] = []

        class RecordingConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, *args, **kwargs):
                executed.append(sql)
                return self._connection.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._connection, name)

        monkeypatch.setattr(
            tdm_schedule_storage.sqlite3,
            "connect",
            lambda *args, **kwargs: RecordingConnection(
                real_connect(*args, **kwargs)
            ),
        )
        assert list(view.iter_hop_identity_records()) == [
            ("d0", "ab", "a", "b", "s000000"),
            ("d0", "bc", "b", "c", "s000001"),
            ("d0", "bd", "b", "d", "s000002"),
        ]
        assert len(executed) == 1


def test_v4_consumer_cursor_wraps_damaged_typed_table_as_validation_error(
    tmp_path,
):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(2))
    with open_tdm_schedule_view(path) as view:
        with closing(sqlite3.connect(view.store_path)) as connection, connection:
            connection.execute("DROP TABLE schedule_entries")
        with pytest.raises(ValidationError, match="typed relation is unreadable"):
            list(view.iter_hop_identity_records())
    with pytest.raises(ValidationError, match="typed relations are unreadable"):
        open_tdm_schedule_view(path, verify_store_hash=False)


@pytest.mark.parametrize(
    ("section", "mutation", "message"),
    [
        (
            "routes",
            lambda value: value["routes"][0].update({"unknown": 1}),
            "typed schedule route is malformed",
        ),
        (
            "routes",
            lambda value: value["routes"][0]["tree_edges"][0].update(
                {"unknown": 1}
            ),
            "typed schedule route edge is malformed",
        ),
        (
            "demand_completions",
            lambda value: value["demand_completions"][0].update(
                {"source_ready_slot": True}
            ),
            "typed schedule completion is malformed",
        ),
    ],
)
def test_v4_writer_rejects_malformed_typed_relations(
    tmp_path, section, mutation, message
):
    del section
    value = _typed_exact_schedule(1)
    mutation(value)
    with pytest.raises(ValidationError, match=message):
        write_tdm_schedule_storage(tmp_path / "schedule.json", value)


def test_v4_typed_route_mutation_breaks_logical_seal(tmp_path):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(3))
    _reseal_typed_store(
        path,
        lambda connection: connection.execute(
            "UPDATE schedule_routes SET net='tampered' WHERE seq=1"
        ),
    )

    with open_tdm_schedule_view(path) as view:
        with pytest.raises(ValidationError, match="logical seal"):
            view.validate_logical_seals()


def test_v4_open_rejects_missing_sink_arrival_row(tmp_path):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(3))
    _reseal_typed_store(
        path,
        lambda connection: connection.execute(
            "DELETE FROM schedule_completion_arrivals "
            "WHERE completion_seq=1 AND seq=0"
        ),
    )

    with pytest.raises(ValidationError, match="coverage disagrees"):
        open_tdm_schedule_view(path)


def test_v4_manifest_rejects_wrong_route_relation_schema(tmp_path):
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, _typed_exact_schedule(2))
    manifest["route_relation"]["schema"] = "unknown/relation"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="relations are malformed"):
        open_tdm_schedule_view(path)


def test_v4_storage_accepts_integer_values_for_semantic_validator(tmp_path):
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

    from tests import schedule_store_fixture
    monkeypatch.setattr(schedule_store_fixture, "write_json", fail_manifest)
    with pytest.raises(RuntimeError, match="injected"):
        write_tdm_schedule_storage(path, updated)

    assert json.loads(path.read_text(encoding="utf-8")) == old_manifest
    assert old_store.exists()
    assert read_json(path) == old_schedule
    assert len(list(tmp_path.glob("schedule.*.sqlite"))) == 2


def test_v4_successful_publication_keeps_previous_generation_readable(tmp_path):
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


def test_v4_rejects_non_content_addressed_artifact_name(tmp_path):
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, _typed_exact_schedule(1))
    manifest["artifact"] = "schedule.sqlite"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="artifact identity"):
        open_tdm_schedule_view(path)


def test_v4_open_rejects_noncontiguous_entry_sequence(tmp_path):
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


def test_v4_open_rejects_wrong_required_index_columns(tmp_path):
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


def test_v4_typed_entry_mutation_breaks_logical_seal_validation(tmp_path):
    expected = _typed_exact_schedule(3)
    path = tmp_path / "schedule.json"
    manifest = write_tdm_schedule_storage(path, expected)
    store = tmp_path / manifest["artifact"]
    with closing(sqlite3.connect(store)) as connection, connection:
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


def test_typed_entry_reader_avoids_python_json_decode(tmp_path, monkeypatch):
    expected = _schedule(3)
    expected['entries'][1]['tdm_ratio'] = 4
    path = tmp_path / 'schedule.json'
    write_tdm_schedule_storage(path, expected)
    with open_tdm_schedule_view(path) as view:
        def reject_decode(*_args, **_kwargs):
            raise AssertionError('typed entry reader decoded JSON in Python')
        monkeypatch.setattr(tdm_schedule_storage.json, 'loads', reject_decode)
        assert list(view['entries']) == expected['entries']


def test_typed_entry_reader_preserves_sequence_across_million_boundary(tmp_path):
    value = _schedule(2)
    value['entries'][0]['id'] = 's999999'
    value['entries'][1]['id'] = 's1000000'
    path = tmp_path / 'schedule.json'
    write_tdm_schedule_storage(path, value)
    with open_tdm_schedule_view(path) as view:
        assert [row['id'] for row in view['entries']] == ['s999999', 's1000000']
        assert [row['id'] for row in view['entries'].iter_by_identity()] == ['s999999', 's1000000']


@pytest.mark.parametrize('mutation', ['slot-type', 'null-ratio'])
def test_typed_entry_fixture_rejects_untyped_protocol_values(tmp_path, mutation):
    value = _schedule(1)
    value['entries'][0]['slot' if mutation == 'slot-type' else 'tdm_ratio'] = True if mutation == 'slot-type' else None
    with pytest.raises(ValidationError, match='malformed|canonical'):
        write_tdm_schedule_storage(tmp_path / 'schedule.json', value)


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


def test_v4_fully_typed_store_is_byte_deterministic(tmp_path):
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


@pytest.mark.parametrize("version", [1, 2, 3, 99])
def test_retired_or_unknown_store_is_rejected_before_sidecar_access(tmp_path, monkeypatch, version):
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(path, _typed_exact_schedule(2))
    manifest = json.loads(path.read_text())
    manifest["schema"] = f"emuflow.tdm-schedule-storage/v{version}"
    path.write_text(json.dumps(manifest))
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported schema opened its sidecar")
    monkeypatch.setattr(tdm_schedule_storage.sqlite3, "connect", forbidden)
    for read in (read_json, tdm_schedule_storage.read_tdm_schedule,
                 open_tdm_schedule_view, tdm_schedule_storage.tdm_schedule_store_path):
        with pytest.raises(ValidationError, match="schedule storage|manifest"):
            read(path)
    destination = tmp_path / "copy/schedule.json"
    with pytest.raises(ValidationError, match="schedule storage|manifest"):
        tdm_schedule_storage.copy_tdm_schedule_storage(path, destination)
    assert not destination.parent.exists()
