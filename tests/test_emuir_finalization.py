"""Private generation edits cannot mutate a published owner or stale its inputs."""
from emuflow_tools.ir.emuir_fixture import write_compact_emuir, write_emuir_fixture

import copy
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from emuflow_tools.ir import emuir_fixture as emuir_compact; from tests import routed_opensta_reference as xilinx_opensta
from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from emuflow.storage.store_publication import bind_store_source
from tests.transport_fanout_reference import buffer_transport_high_fanout_records
from tests.routed_timing_adapter import XILINX_ROUTED_TIMING_PAYLOAD_FORMAT
from tests.test_transport_fanout import _transport_ir
from tests.test_xilinx_opensta import _indexed_routed_fixture


@pytest.mark.parametrize(
    "failure", [None, "after-transform", "changed-source", "native"]
)
def test_fanout_finishes_before_the_only_publication(tmp_path, failure):
    output = tmp_path / "transport.emuir.sqlite"
    write_emuir_fixture(output, (_transport_ir(2)).value)
    before = output.read_bytes()
    source = tmp_path / "transport.sv"
    source.write_text("module transport; endmodule\n")
    sources = (bind_store_source(source, "yosys-source-document-v1"),)
    reports = []
    statements = []
    publish = emuir_compact.publish_sqlite_store

    def finalize(connection):
        assert output.read_bytes() == before

        def audit(action, name, _argument, _database, _trigger):
            if action == sqlite3.SQLITE_PRAGMA:
                assert name.lower() not in {"integrity_check", "quick_check"}
            return sqlite3.SQLITE_OK

        connection.set_authorizer(audit)
        connection.set_trace_callback(statements.append)
        reports.append(buffer_transport_high_fanout_records(connection, max_fanout=4))
        assert output.read_bytes() == before
        if failure == "after-transform":
            raise ValidationError("finalizer rejected")
        if failure == "changed-source":
            source.chmod(source.stat().st_mode ^ 0o100)

    def adopt(*args, **kwargs):
        assert len(reports) == 1
        assert output.read_bytes() == before
        if failure == "native":
            raise ValidationError("native rejected")
        return publish(*args, **kwargs)

    with patch.object(
        emuir_compact, "publish_sqlite_store", side_effect=adopt
    ) as publisher:
        if failure is None:
            write_compact_emuir(
                output,
                _transport_ir(17).value,
                source_bindings=sources,
                _finalize=finalize,
            )
            publisher.assert_called_once()
        else:
            with pytest.raises(ValidationError, match="rejected|generation changed"):
                write_compact_emuir(
                    output,
                    _transport_ir(17).value,
                    source_bindings=sources,
                    _finalize=finalize,
                )
            assert output.read_bytes() == before
    assert reports[0]["summary"]["buffer_cells"] > 0
    assert any("INSERT INTO cell_records" in statement for statement in statements)
    assert not list(tmp_path.glob("*.tmp"))
    if failure is None:
        ir = EmuIR.load(output)
        assert len(ir.value["instances"]) == 18 + reports[0]["summary"]["buffer_cells"]


@pytest.mark.parametrize("failure", ["native", "mapped", "timing", "payload"])
def test_routed_normalization_rejects_changes_without_mutating_previous_owner(tmp_path, failure):
    from tests.routed_opensta_stage_reference import NativeToolProcess
    mapped, timing = _indexed_routed_fixture(tmp_path)
    output = tmp_path / "routed.emuir.sqlite"
    xilinx_opensta.build_xilinx_routed_opensta_indexed_inputs(mapped, timing, output, clocks={"clk": 10.0})
    library = output.with_name(output.name + ".route-delays.lib")
    before = {path: path.read_bytes() for path in (output, library)}
    finish = NativeToolProcess.finish
    def fail_after_native(process):
        result = finish(process)
        for path, content in before.items():
            assert path.read_bytes() == content
        if failure == "native":
            result.returncode = 17
        else:
            payload = timing.parent / json.loads(timing.read_text())["payloads"]["endpoints"]["path"]
            changed = {"mapped": mapped, "timing": timing, "payload": payload}[failure]
            changed.chmod(changed.stat().st_mode ^ 0o100)
        return result
    with patch.object(NativeToolProcess, "finish", fail_after_native):
        with pytest.raises(ValidationError, match="staging failed|generation changed"):
            xilinx_opensta.build_xilinx_routed_opensta_indexed_inputs(mapped, timing, output, clocks={"clk": 10.0})
    for path, content in before.items():
        assert path.read_bytes() == content
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failure", ["native", "source", "selector"])
def test_chimew_relocation_uses_original_input_bindings_and_preserves_previous_commit(
    tmp_path, failure
):
    from emuflow_tools.transport.chimew import chimew_bank_channel_storage as storage
    from emuflow.storage import store_publication as publication
    from tests.test_chimew_bank_channel import ChimewBankChannelTest

    case = ChimewBankChannelTest()
    case.setUp()
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    storage.write_chimew_bank_channel_storage(source, case.document).close()
    storage.copy_chimew_bank_channel_storage(source, target)
    with storage.BankChannelInputView(target) as view:
        old_payload = view.store_path
    before = target.read_bytes(), old_payload.read_bytes()
    replacement = copy.deepcopy(case.document)
    replacement["provenance"]["grouping_sha256"] = "d" * 64
    storage.write_chimew_bank_channel_storage(source, replacement).close()
    publish, write = storage.publish_sqlite_store, publication.write_json

    def adopt(*args, **kwargs):
        assert len(kwargs["sources"]) == 2
        if failure == "native":
            raise ValidationError("native rejected")
        if failure == "source":
            source.chmod(source.stat().st_mode ^ 0o100)
        return publish(*args, **kwargs)

    def selector(path, *args, **kwargs):
        result = write(path, *args, **kwargs)
        if path == target and failure == "selector":
            raise OSError("selector rejected")
        return result

    with (
        patch.object(storage, "publish_sqlite_store", side_effect=adopt),
        patch.object(publication, "write_json", side_effect=selector),
        patch.object(
            storage.BankChannelInputView,
            "materialize",
            side_effect=AssertionError("graph reconstruction"),
        ),
    ):
        with pytest.raises(
            (ValidationError, OSError), match="rejected|generation changed"
        ):
            storage.copy_chimew_bank_channel_storage(source, target)
    assert target.read_bytes() == before[0] and old_payload.read_bytes() == before[1]
    assert list(tmp_path.glob("target.*.sqlite")) == [old_payload]
    assert not list(tmp_path.glob("*.tmp"))
