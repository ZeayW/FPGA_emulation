"""Typed bank owner parity, storage rejection and publication boundaries."""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.transport.chimew.chimew_bank_channel import evaluate_chimew_bank_channel_assignment as assign
from emuflow_tools.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage
from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import canonical_bank_channel_report_sha256, read_chimew_bank_channel_report
from tests.chimew_bank_channel_reference import evaluate_chimew_bank_channel_assignment as reference
from tests.native_build import emuflow_native
from tests.test_chimew_bank_channel import member


def document():
    from tests.test_chimew_bank_channel import ChimewBankChannelTest
    case = ChimewBankChannelTest()
    case.setUp()
    return copy.deepcopy(case.document)


@pytest.mark.parametrize("variant", ["standard", "weighted", "unicode", "v1", "partial", "bidirectional", "uppercase"])
def test_native_typed_report_matches_independent_prior_certificate_oracle(tmp_path, variant):
    raw = document()
    if variant == "weighted":
        for i, group in enumerate(raw["groups"]):
            for m in group["members"]:
                m["timing_weight"] = 1.0 + 0.1 * i
    if variant == "unicode":
        raw["design"] = "布局☃"
        for group in raw["groups"]:
            group["id"] += "☃"
    if variant == "v1":
        raw["schema"] = "emuflow.chimew-bank-channel-input/v1"
    if variant == "partial":
        raw["groups"] = raw["groups"][:1]
        raw["metrics"].update(groups=1, signals=1, fanins=1)
    if variant == "bidirectional":
        raw["groups"] = [{"id": "both", "domain": "AB", "kind": "tdm_group",
                          "direction": "bidirectional", "members": [
                              {**member("forward", 0.0, 0.0), "direction": "a_to_b"},
                              {"id": "reverse", "direction": "b_to_a",
                               "fanout": {"x": 100.0, "y": 20.0},
                               "fanins": [{"x": 0.0, "y": 20.0}]}]}]
        raw["metrics"].update(groups=1, signals=2, fanins=2, bidirectional_bundles=1)
    if variant == "uppercase":
        for key in ("grouping_sha256", "placement_sha256", "architecture_sha256"):
            raw["provenance"][key] = raw["provenance"][key].upper()
    exe = str(emuflow_native())
    expected = reference(raw, executable=exe)
    with write_chimew_bank_channel_storage(tmp_path / "input.json", raw) as source:
        with assign(source, executable=exe, output_path=tmp_path / "report.json") as actual:
            assert actual.materialize() == expected
            assert canonical_bank_channel_report_sha256(actual) == canonical_bank_channel_report_sha256(expected)


@pytest.mark.parametrize("failure", ["version", "application", "identity", "sequence", "schema", "header", "relation", "logical"])
def test_resealed_invalid_input_leaves_previous_published_report_intact(tmp_path, failure):
    source_path, output = tmp_path / "input.json", tmp_path / "report.json"
    with write_chimew_bank_channel_storage(source_path, document()) as source:
        with assign(source, output_path=output) as old:
            old_store = old.store_path
    before, payload = output.read_bytes(), old_store.read_bytes()
    selector = json.loads(source_path.read_text())
    store = source_path.parent / selector["artifact"]
    if failure in ("version", "application", "identity", "sequence", "schema"):
        with closing(sqlite3.connect(store)) as db, db:
            if failure == "version":
                db.execute("PRAGMA user_version=2")
            elif failure == "application":
                db.execute("PRAGMA application_id=1")
            elif failure == "identity":
                db.execute("UPDATE groups SET group_id='resealed-wrong' WHERE seq=0")
            elif failure == "sequence":
                db.execute("UPDATE groups SET seq=20 WHERE seq=0")
            else:
                db.execute("CREATE TABLE extra(value TEXT)")
        digest = hashlib.sha256(store.read_bytes()).hexdigest()
        renamed = store.with_name(f"input.{digest}.sqlite")
        store.rename(renamed)
        selector.update(artifact=renamed.name, store_sha256=digest, store_bytes=renamed.stat().st_size)
    elif failure == "header":
        selector["problem_header"]["design"] = "different-design"
    elif failure == "relation":
        selector["groups"]["sha256"] = "0" * 64
    else:
        selector["logical_sha256"] = "0" * 64
    source_path.write_text(json.dumps(selector))
    receipt = tmp_path / "operation.json"
    result = subprocess.run([str(emuflow_native()), "--report", str(receipt), "phase6",
                             "assign-bank-store", str(source_path), str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    operation = json.loads(receipt.read_text())
    assert operation["status"] == "fail" and operation["outputs"] == []
    assert output.read_bytes() == before
    assert old_store.read_bytes() == payload
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob(".chimew-bank-*"))


def test_production_host_rejects_raw_inputs_missing_output_and_alternate_executable(tmp_path):
    with pytest.raises(ValidationError, match="typed bank input"):
        assign(document(), output_path=tmp_path / "report.json")
    with write_chimew_bank_channel_storage(tmp_path / "input.json", document()) as source:
        with pytest.raises(ValidationError, match="output selector"):
            assign(source)
        with pytest.raises(ValidationError, match="unified native executable"):
            assign(source, executable="/bin/false", output_path=tmp_path / "report.json")


def test_native_writer_waits_for_old_report_reader_before_replacement_and_gc(tmp_path):
    with write_chimew_bank_channel_storage(tmp_path / "input1.json", document()) as first:
        old = assign(first, output_path=tmp_path / "report.json")
    old_store = old.store_path
    next_input = document()
    next_input["design"] = "new-generation"
    started, completed = threading.Event(), threading.Event()
    failures = []
    def replace():
        try:
            with write_chimew_bank_channel_storage(tmp_path / "input2.json", next_input) as source:
                started.set()
                with assign(source, output_path=tmp_path / "report.json"):
                    pass
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()
    thread = threading.Thread(target=replace)
    thread.start()
    assert started.wait(5)
    assert not completed.wait(0.15)
    assert old_store.is_file() and old["design"] != "new-generation"
    old.close()
    assert completed.wait(15)
    thread.join()
    assert not failures and not old_store.exists()
    with read_chimew_bank_channel_report(tmp_path / "report.json") as current:
        assert current["design"] == "new-generation"


@pytest.mark.parametrize("alias", ["input", "output"])
def test_native_cli_report_cannot_overwrite_assignment_sources_or_result(tmp_path, alias):
    source_path, output = tmp_path / "input.json", tmp_path / "report.json"
    with write_chimew_bank_channel_storage(source_path, document()) as source:
        with assign(source, output_path=output):
            pass
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    report = source_path if alias == "input" else output
    result = subprocess.run([str(emuflow_native()), "--report", str(report), "phase6",
        "assign-bank-store", str(source_path), str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
