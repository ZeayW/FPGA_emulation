from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
"""Shared publication contracts; phase tests retain independent semantics."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from contextlib import closing
from collections.abc import Sequence
from unittest.mock import patch

import pytest

from emuflow.storage import store_publication
from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation
from tests.pin_plan_storage_fixture import write_pin_plan_storage
from tests.chimew_binding_storage_fixture import write_chimew_binding_storage
from emuflow.transport.transport_storage import DDL
from tests.native_build import emuflow_native
from tests.test_pin_plan_storage import _plan


def private_store(root, schema="pin-plan-store-v1"):
    with patch.object(
        store_publication,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        if schema == "pin-plan-store-v1":
            source = write_pin_plan_storage(root / "plan.json", _plan()).store_path
        elif schema == "chimew-binding-store-v1":
            source = write_chimew_binding_storage(
                root / "binding.json",
                {
                    "schema": "emuflow.chimew-phase6-electrical-binding/v3",
                    "entries": [],
                },
            ).store_path
        elif schema == "position-hints-store-v1":
            from tests.position_hints_storage_fixture import write_position_hints_storage
            from tests.test_position_hints_storage import _positions

            source = write_position_hints_storage(
                root / "positions.json", _positions()
            ).store_path
        elif schema == "chimew-bank-input-store-v1":
            from emuflow.transport.chimew.chimew_bank_channel_storage import (
                write_chimew_bank_channel_storage,
            )
            from tests.test_chimew_bank_channel import ChimewBankChannelTest

            case = ChimewBankChannelTest()
            case.setUp()
            with write_chimew_bank_channel_storage(
                root / "bank-input.json", case.document
            ) as view:
                source = view.store_path
        elif schema == "chimew-bank-report-store-v1":
            from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
            from tests.test_chimew_bank_channel import ChimewBankChannelTest

            case = ChimewBankChannelTest()
            case.setUp()
            with evaluate_chimew_bank_channel_assignment(
                case.document,
                executable=str(emuflow_native()),
                output_path=root / "bank-report.json",
            ) as view:
                source = view.store_path
        else:
            # Envelope-only fixture. Real transport construction and electrical
            # equivalence are exercised by the Phase 6 tests, not inferred here.
            source = root / "transport.sqlite"
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("PRAGMA application_id=0x454d5558")
                db.execute("PRAGMA user_version=1")
                db.executescript(DDL)
                db.executemany(
                    "INSERT INTO metadata VALUES (?,?)",
                    [
                        ("design", '"fixture"'),
                        ("platform", '"board"'),
                        ("frame_slots", "4"),
                    ],
                )
    temporary = root / ".owned.sqlite.tmp"
    shutil.copyfile(source, temporary)
    return source, temporary


def publish(temporary, output, schema, sources=()):
    with patch.object(
        store_publication,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        return store_publication.publish_sqlite_store(
            temporary, output, schema, sources=sources
        )


@pytest.mark.parametrize(
    "schema",
    [
        "transport-store-v1",
        "pin-plan-store-v1",
        "chimew-binding-store-v1",
        "position-hints-store-v1",
        "chimew-bank-report-store-v1",
        "chimew-bank-input-store-v1",
    ],
)
def test_registered_store_adoption_preserves_bytes_and_inode(tmp_path, schema):
    source, temporary = private_store(tmp_path, schema)
    expected, inode = temporary.read_bytes(), temporary.stat().st_ino
    original_generation = native_file_generation(source)
    output = tmp_path / "published.sqlite"
    output.write_text("previous publication")
    result = publish(
        temporary, output, schema, [store_publication.bind_store_source(source, schema)]
    )
    assert result["status"] == "pass" and result["stage"] == "native.publish-store"
    assert output.stat().st_ino == inode and output.read_bytes() == expected
    assert result["outputs"][0]["generation"]["inode"] == inode
    assert (
        len(result["inputs"]) == 2
    )  # Request and persistent source, not consumed staging.
    assert native_file_generation(source) == original_generation
    assert not temporary.exists() and not list(tmp_path.glob(".store-publication-*"))


def test_verified_store_seal_is_scoped_to_the_exact_artifact_generation(tmp_path):
    source, other = private_store(tmp_path)
    owner = store_publication.VerifiedStoreFile(source, "pin-plan-store-v1")
    with patch.object(
        store_publication,
        "file_sha256",
        side_effect=AssertionError("duplicate full-file hash"),
    ):
        owner.require(source, "pin-plan-store-v1", owner.bytes, owner.sha256)
        with pytest.raises(ValidationError, match="another artifact"):
            owner.require(other, "pin-plan-store-v1", owner.bytes, owner.sha256)
        with pytest.raises(ValidationError, match="another artifact"):
            owner.require(source, "transport-store-v1", owner.bytes, owner.sha256)
        with pytest.raises(ValidationError, match="another artifact"):
            owner.require(source, "pin-plan-store-v1", owner.bytes, "0" * 64)
        source.write_bytes(source.read_bytes() + b"\n")
        with pytest.raises(ValidationError, match="generation changed"):
            owner.require(source, "pin-plan-store-v1", owner.bytes, owner.sha256)


@pytest.mark.parametrize(
    "failure",
    [
        "schema",
        "version",
        "logical_schema",
        "sidecar",
        "source_changed",
        "source_missing",
        "different_directory",
        "hardlink",
        "symlink",
        "destination_sidecar",
    ],
)
def test_publication_failure_preserves_previous_store_and_source(tmp_path, failure):
    source, temporary = private_store(tmp_path)
    sources = [store_publication.bind_store_source(source, "pin-plan-store-v1")]
    schema = "pin-plan-store-v1"
    if failure == "schema":
        schema = "retired-store-v0"
    elif failure in {"version", "logical_schema"}:
        with closing(sqlite3.connect(temporary)) as db, db:
            if failure == "version":
                db.execute("PRAGMA user_version=99")
            else:
                db.execute("UPDATE metadata SET value='{}' WHERE key='header'")
    elif failure == "sidecar":
        temporary.with_name(temporary.name + "-wal").write_text("active writer")
    elif failure == "source_changed":
        source.write_bytes(source.read_bytes() + b"\n")
    elif failure == "source_missing":
        source.unlink()
    elif failure == "different_directory":
        subdir = tmp_path / "other"
        subdir.mkdir()
        temporary = temporary.rename(subdir / temporary.name)
    elif failure in {"hardlink", "symlink"}:
        temporary.unlink()
        if failure == "hardlink":
            os.link(source, temporary)
        else:
            temporary.symlink_to(source)
    output = tmp_path / "published.sqlite"
    output.write_text("previous publication")
    if failure == "destination_sidecar":
        output.with_name(output.name + "-wal").write_text("active destination writer")
    with pytest.raises(ValidationError):
        publish(temporary, output, schema, sources)
    assert output.read_text() == "previous publication"
    assert temporary.exists() and not list(tmp_path.glob(".store-publication-*"))


@pytest.mark.parametrize(
    "failure",
    [
        "version",
        "crc",
        "truncated",
        "staging_generation",
        "source_generation",
        "alias_source",
        "alias_staging",
        "alias_request",
        "report_source",
        "report_staging",
        "report_output",
        "existing_report",
    ],
)
def test_checked_publication_request_rejection_is_non_destructive(tmp_path, failure):
    source, temporary = private_store(tmp_path)
    request = tmp_path / "request.bin"
    writer = CheckedRequestWriter(request)
    writer.put("QI", 0x31505351464D5545, 2 if failure == "version" else 1)
    writer.text("pin-plan-store-v1")
    writer.text(str(temporary))
    staged_generation = list(native_file_generation(temporary))
    if failure == "staging_generation":
        staged_generation[2] += 1
    writer.put("3Q4q", *staged_generation)
    writer.put("I", 1)
    writer.text(str(source))
    writer.text("pin-plan-store-v1")
    generation = list(native_file_generation(source))
    if failure == "source_generation":
        generation[2] += 1
    writer.put("3Q4q", *generation)
    writer.finish()
    writer.close()
    if failure in {"crc", "truncated"}:
        data = bytearray(request.read_bytes())
        if failure == "crc":
            data[-1] ^= 1
        else:
            data = data[:-1]
        request.write_bytes(data)
    output = tmp_path / "published.sqlite"
    output.write_text("previous publication")
    output = {
        "alias_source": source,
        "alias_staging": temporary,
        "alias_request": request,
    }.get(failure, output)
    report = {
        "report_source": source,
        "report_staging": temporary,
        "report_output": output,
    }.get(failure, tmp_path / "result.json")
    if failure == "existing_report":
        report.write_text("previous report")
    before = {
        path: path.read_bytes()
        for path in {source, temporary, request, output, report}
        if path.exists()
    }
    completed = subprocess.run(
        [
            str(emuflow_native()),
            "--report",
            str(report),
            "publish-store",
            str(request),
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("kind", ["pin", "binding"])
def test_producer_rejects_source_changed_during_record_generation(tmp_path, kind):
    source, _temporary = private_store(tmp_path)
    bound = store_publication.bind_store_source(source, "pin-plan-store-v1")
    output = tmp_path / "output.json"
    output.write_text('{"previous":true}')

    def entries():
        source.write_bytes(source.read_bytes() + b"\n")
        if kind == "pin":
            yield _plan()["entries"][0]

    class Records(Sequence):
        def __len__(self):
            return 1

        def __iter__(self):
            return entries()

        def __getitem__(self, key):
            raise AssertionError("record iterator must remain streamed")

    with patch.object(
        store_publication,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        with pytest.raises(ValidationError, match="generation changed"):
            if kind == "pin":
                write_pin_plan_storage(
                    output, {**_plan(), "entries": Records()}, source_bindings=[bound]
                )
            else:
                write_chimew_binding_storage(
                    output,
                    {
                        "schema": "emuflow.chimew-phase6-electrical-binding/v3",
                        "entries": entries(),
                    },
                    source_bindings=[bound],
                )
    assert json.loads(output.read_text()) == {"previous": True}
    assert not list(tmp_path.glob(".output.*.sqlite.tmp"))


@pytest.mark.parametrize(
    "kind", ["pin", "binding", "schedule", "position", "bank_report", "bank_input"]
)
@pytest.mark.parametrize("changed", ["selector", "store"])
def test_indexed_views_bind_original_generations_instead_of_taking_a_new_snapshot(
    tmp_path, kind, changed
):
    with patch.object(
        store_publication,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        if kind == "pin":
            view = write_pin_plan_storage(tmp_path / "plan.json", _plan())
            schema = "pin-plan-store-v1"
        elif kind == "binding":
            view = write_chimew_binding_storage(
                tmp_path / "binding.json",
                {
                    "schema": "emuflow.chimew-phase6-electrical-binding/v3",
                    "entries": [],
                },
            )
            schema = "chimew-binding-store-v1"
        elif kind == "position":
            from tests.position_hints_storage_fixture import write_position_hints_storage
            from tests.test_position_hints_storage import _positions

            view = write_position_hints_storage(
                tmp_path / "positions.json", _positions()
            )
            schema = "position-hints-store-v1"
        elif kind == "bank_input":
            from emuflow.transport.chimew.chimew_bank_channel_storage import (
                write_chimew_bank_channel_storage,
            )
            from tests.test_chimew_bank_channel import ChimewBankChannelTest

            case = ChimewBankChannelTest()
            case.setUp()
            view = write_chimew_bank_channel_storage(
                tmp_path / "bank-input.json", case.document
            )
            schema = "chimew-bank-input-store-v1"
        elif kind == "bank_report":
            from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
            from tests.test_chimew_bank_channel import ChimewBankChannelTest

            case = ChimewBankChannelTest()
            case.setUp()
            view = evaluate_chimew_bank_channel_assignment(
                case.document,
                executable=str(emuflow_native()),
                output_path=tmp_path / "bank-report.json",
            )
            schema = "chimew-bank-report-store-v1"
        else:
            from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
            from tests.schedule_store_fixture import write_tdm_schedule_storage
            from tests.test_phase6 import Phase6Test

            case = Phase6Test()
            case.setUp()
            write_tdm_schedule_storage(tmp_path / "schedule.json", case.schedule)
            view = read_tdm_schedule(tmp_path / "schedule.json")
            schema = "tdm-schedule-store-v4"
    bound = store_publication.bind_view_sources((view, schema))
    assert {source.path for source in bound} == {view.manifest_path, view.store_path}
    target = view.manifest_path if changed == "selector" else view.store_path
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValidationError, match="generation changed"):
        store_publication.bind_view_sources((view, schema))


@pytest.mark.parametrize("kind", ["pin", "binding"])
def test_existing_identical_store_does_not_bypass_source_checks_or_leak_staging(
    tmp_path, kind
):
    from emuflow.transport.pin_plan_storage import copy_pin_plan_storage
    from emuflow.transport.chimew.chimew_binding_storage import copy_chimew_binding_storage

    source, _temporary = private_store(tmp_path)
    bound = store_publication.bind_store_source(source, "pin-plan-store-v1")
    output = tmp_path / "output.json"
    writer, copier, document = (
        (write_pin_plan_storage, copy_pin_plan_storage, _plan())
        if kind == "pin"
        else (write_chimew_binding_storage, copy_chimew_binding_storage,
              {"schema": "emuflow.chimew-phase6-electrical-binding/v3", "entries": []})
    )
    owner = writer(tmp_path / "owner.json", document)
    copier(owner, output, source_bindings=[bound])
    previous = output.read_bytes()
    payload = output.with_name(json.loads(previous)["artifact"])
    previous_payload = payload.read_bytes()
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValidationError, match="generation changed"):
        copier(owner, output, source_bindings=[bound])
    assert output.read_bytes() == previous and payload.read_bytes() == previous_payload
    assert not list(tmp_path.glob(".output.*.sqlite.tmp"))


def test_indexed_bank_input_republication_streams_without_materializing_or_waiting_on_itself(
    tmp_path,
):
    from emuflow.transport.chimew.chimew_bank_channel_storage import (
        BankChannelInputView,
        write_chimew_bank_channel_storage,
    )
    from tests.test_chimew_bank_channel import ChimewBankChannelTest

    case = ChimewBankChannelTest()
    case.setUp()
    with patch.object(
        store_publication,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        with write_chimew_bank_channel_storage(
            tmp_path / "input.json", case.document
        ) as original:
            with patch.object(
                BankChannelInputView,
                "materialize",
                side_effect=AssertionError("whole input graph"),
            ):
                with write_chimew_bank_channel_storage(
                    tmp_path / "copy.json", original
                ) as copied:
                    assert copied.logical_sha256 == original.logical_sha256
                    assert list(copied["groups"]) == list(original["groups"])
                    assert list(copied["bank_pairs"]) == list(original["bank_pairs"])
                with pytest.raises(ValidationError, match="active view"):
                    write_chimew_bank_channel_storage(original.manifest_path, original)
