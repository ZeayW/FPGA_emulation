"""Late publication failures must preserve every retained Phase 6 typed owner."""

import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow import store_publication as common
from emuflow.errors import ValidationError
from tests.native_build import emuflow_native


KINDS = (
    "pin_plan",
    "position_hints",
    "chimew_binding",
    "chimew_bank_channel",
    "chimew_bank_channel_report",
)


def prepare(kind):
    module = importlib.import_module("emuflow." + kind + "_storage")
    if kind == "pin_plan":
        from tests.test_pin_plan_storage import _plan

        document = _plan()
    elif kind == "position_hints":
        from tests.test_position_hints_storage import _positions

        document = _positions()
    elif kind == "chimew_binding":
        document = {
            "schema": "emuflow.chimew-phase6-electrical-binding/v3",
            "entries": [],
        }
    else:
        from tests.test_chimew_bank_channel import ChimewBankChannelTest

        case = ChimewBankChannelTest()
        case.setUp()
        document = case.document
        if kind.endswith("_report"):
            from emuflow.chimew_bank_channel import (
                evaluate_chimew_bank_channel_assignment,
            )

            document = evaluate_chimew_bank_channel_assignment(
                document, executable=str(emuflow_native())
            )
    writer = getattr(module, "write_" + kind + "_storage")

    def produce(path, generation=0, sources=()):
        # A bounded header change leaves algorithm/records untouched but creates
        # a distinct generation, so a dangling selector cannot hide in reuse.
        value = {**document, "design": "generation-" + str(generation)}
        return writer(path, value, source_bindings=sources)

    return module, produce


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "failure", ["after-replace", "changed-source", "partial-backup", "failed-rollback"]
)
def test_late_auxiliary_selector_failures_preserve_prior_or_recoverable_generations(
    tmp_path, kind, failure
):
    module, produce = prepare(kind)
    path, source = tmp_path / "result.json", tmp_path / "source.txt"
    source.write_text("original input")
    old = produce(path)
    previous = path.read_bytes()
    previous_store = old.store_path
    previous_bytes = previous_store.read_bytes()
    if callable(getattr(old, "close", None)):
        old.close()
    bindings = (common.bind_store_source(source, "sta-source-document-v1"),)
    write, replace, copy = common.write_json, os.replace, common.shutil.copyfile

    def late_write(target, value, **kwargs):
        result = write(target, value, **kwargs)
        if Path(target) == path:
            if failure == "changed-source":
                source.write_text("changed after selector replacement")
            elif failure in ("after-replace", "failed-rollback"):
                raise OSError("injected late selector failure")
        return result

    def partial_backup(first, second, *args, **kwargs):
        if failure == "partial-backup" and Path(first) == path:
            Path(second).write_bytes(b"{incomplete")
            raise OSError("injected partial backup failure")
        return copy(first, second, *args, **kwargs)

    def failed_restore(first, second):
        if (
            failure == "failed-rollback"
            and Path(second) == path
            and str(first).endswith(".previous")
        ):
            raise OSError("injected selector recovery failure")
        return replace(first, second)

    expected = ValidationError if failure == "changed-source" else OSError
    with (
        patch.object(module, "write_json", side_effect=late_write, create=True),
        patch.object(common, "write_json", side_effect=late_write),
        patch.object(common.shutil, "copyfile", side_effect=partial_backup),
        patch.object(common.os, "replace", side_effect=failed_restore),
    ):
        with pytest.raises(expected, match="generation changed|injected"):
            replacement = produce(path, generation=1, sources=bindings)
            if callable(getattr(replacement, "close", None)):
                replacement.close()
    assert previous_store.read_bytes() == previous_bytes
    recovery = list(tmp_path.glob("*.previous"))
    if failure == "failed-rollback":
        assert len(recovery) == 1 and recovery[0].read_bytes() == previous
        assert path.read_bytes() != previous
        assert (tmp_path / json.loads(path.read_text())["artifact"]).is_file()
    else:
        assert path.read_bytes() == previous and not recovery
        assert set(tmp_path.glob("*.sqlite")) == {previous_store}
        assert not list(tmp_path.glob("*.restore"))


def test_common_commit_closes_validated_reader_before_rejecting_changed_source(
    tmp_path,
):
    import sqlite3
    from contextlib import closing

    source, old, new, selector = (
        tmp_path / name for name in ("input", "old.sqlite", "new.sqlite", "result.json")
    )
    source.write_text("source")
    old.write_text("old retained payload")
    selector.write_text(json.dumps({"artifact": old.name}))
    previous = selector.read_bytes()
    with closing(sqlite3.connect(new)) as db, db:
        db.execute("CREATE TABLE facts(k INTEGER PRIMARY KEY)")
    reader = sqlite3.connect(new)

    def validate():
        assert reader.execute("SELECT count(*) FROM facts").fetchone() == (0,)
        source.write_text("changed during independent validation")
        return reader

    binding = common.bind_store_source(source, "sta-source-document-v1")
    with pytest.raises(ValidationError, match="generation changed"):
        common.commit_store_selector(
            selector,
            {"artifact": new.name},
            iter((binding,)),
            new,
            True,
            validate=validate,
        )
    assert selector.read_bytes() == previous and old.is_file() and not new.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reader.execute("SELECT 1")


@pytest.mark.parametrize("failure", ["after-replace", "changed-source"])
def test_chimew_input_relocation_uses_the_same_source_bound_selector_commit(
    tmp_path, failure
):
    module, produce = prepare("chimew_bank_channel")
    source, destination = tmp_path / "source.json", tmp_path / "copy.json"
    with produce(source, generation=1):
        pass
    with produce(destination, generation=0) as previous:
        old = destination.read_bytes()
        old_store = previous.store_path
    before = set(tmp_path.glob("*.sqlite"))
    write = common.write_json

    def late_write(path, value, **kwargs):
        result = write(path, value, **kwargs)
        if Path(path) == destination:
            if failure == "after-replace":
                raise OSError("injected relocated selector failure")
            source.chmod(source.stat().st_mode ^ 0o100)
        return result

    expected = OSError if failure == "after-replace" else ValidationError
    with patch.object(common, "write_json", side_effect=late_write):
        with pytest.raises(expected, match="injected|generation changed"):
            module.copy_chimew_bank_channel_storage(source, destination)
    assert destination.read_bytes() == old and old_store.is_file()
    assert set(tmp_path.glob("*.sqlite")) == before
    assert not list(tmp_path.glob("*.previous"))


def test_failed_validated_reader_close_keeps_both_generations_and_recovery(tmp_path):
    source, old, new, selector = (
        tmp_path / name for name in ("input", "old.sqlite", "new.sqlite", "result.json")
    )
    source.write_text("original")
    old.write_text("old payload")
    new.write_text("new payload")
    selector.write_text(json.dumps({"artifact": old.name}))
    previous = selector.read_bytes()
    binding = common.bind_store_source(source, "sta-source-document-v1")

    class UnreleasedReader:
        def close(self):
            raise OSError("injected reader close failure")

    def validate():
        source.write_text("changed while validating")
        return UnreleasedReader()

    with pytest.raises(OSError, match="reader close failure"):
        common.commit_store_selector(
            selector, {"artifact": new.name}, (binding,), new, True, validate=validate
        )
    assert old.is_file() and new.is_file()
    backups = list(tmp_path.glob("*.previous"))
    assert len(backups) == 1 and backups[0].read_bytes() == previous
    assert json.loads(selector.read_text())["artifact"] == new.name
