"""Source-bound publication failures must preserve the last physical evidence."""

import importlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError


KINDS = (
    "boundary_identity",
    "boundary_timing",
    "logic_segment_identity",
    "logic_segment_timing",
    "local_path_timing",
)


def producer(
    kind, path, *, generation=0, source_bindings=(), before_records=None, **options
):
    module = importlib.import_module("emuflow." + kind + "_storage")
    if kind.startswith("boundary_"):
        fixture = importlib.import_module("tests.test_" + kind + "_storage")
        header = fixture._header()
        records = [fixture._record("tx", "tx", 0), fixture._record("rx", "rx", 1)]
    elif kind == "local_path_timing":
        from tests.test_local_path_timing_storage import _header, _record

        header = _header()
        records = [_record("a", 1.0), _record("b", 2.0)]
    else:
        header = {
            "schema": "emuflow." + kind.replace("_", "-") + "/v1",
            "status": "pass",
            "design": "dut",
            "platform": "board",
            "fpga": "fpga0",
            "provider": "fixture",
            "coverage": {},
            "system_routes_sha256": "a" * 64,
            "tdm_schedule_sha256": "b" * 64,
        }
        records = [
            {
                "id": "logic0",
                "kind": "launch",
                "system_path": "path0",
                "member_path": "member0",
                "cut_index": 0,
                "fpga": "fpga0",
                "replace_tx_endpoint": "tx0",
                "start_pin": "start0",
                "end_pin": "end0",
            }
        ]
        if kind == "logic_segment_timing":
            header["qualification"] = "endpoint-exact"
            records[0]["delay_ns"] = 1.25
    header["provider"] = f"fixture-{generation}"

    def stream():
        if before_records:
            before_records()
        yield from records

    writer = getattr(module, "write_" + kind + "_storage")
    if kind.startswith("logic_"):
        return writer(
            path,
            header=header,
            segments=stream(),
            source_bindings=source_bindings,
            **options,
        )
    return writer(path, header, stream(), source_bindings=source_bindings)


@pytest.mark.parametrize("companion", [False, True])
def test_logic_identity_partial_selector_backup_keeps_prior_owner_and_query(
    tmp_path, companion
):
    from emuflow import store_publication as module

    path = tmp_path / "identity.json"
    with producer("logic_segment_identity", path) as prior:
        previous = path.read_bytes()
        store = prior.store_path
        previous_store = store.read_bytes()
    query, staged = tmp_path / "query.tsv", tmp_path / ".query.publish.tmp"
    query.write_text("previous query\n")
    staged.write_text("replacement query\n")
    options = {"sidecar_publication": (staged, query)} if companion else {}
    copy = module.shutil.copyfile

    def fail_partial(source, target, *args, **kwargs):
        if Path(source) == path:
            Path(target).write_bytes(b"{partial")
            raise OSError("injected incomplete selector backup")
        return copy(source, target, *args, **kwargs)

    with patch.object(module.shutil, "copyfile", side_effect=fail_partial):
        with pytest.raises(OSError, match="incomplete selector backup"):
            producer("logic_segment_identity", path, generation=1, **options)
    assert path.read_bytes() == previous
    assert store.read_bytes() == previous_store
    assert set(tmp_path.glob("*.sqlite")) == {store}
    assert query.read_text() == "previous query\n"
    assert not list(tmp_path.glob("*.previous"))
    assert not list(tmp_path.glob("*.rollback.tmp"))
    if companion:
        assert not staged.exists()


@pytest.mark.parametrize("when", ["before", "after", "restore", "directory"])
def test_logic_identity_shared_selector_commit_preserves_recovery_generations(
    tmp_path, when
):
    from emuflow import store_publication as common

    path = tmp_path / "identity.json"
    with producer("logic_segment_identity", path) as prior:
        previous = path.read_bytes()
        previous_store = prior.store_path
    write, replace, fsync = common.write_json, common.os.replace, common.os.fsync
    directory_failed = False

    def reject_write(target, value, **kwargs):
        if Path(target) != path or when == "directory":
            return write(target, value, **kwargs)
        if when != "before":
            write(target, value, **kwargs)
        raise OSError("injected selector commit error")

    def reject_restore(source, target):
        if (
            when == "restore"
            and Path(target) == path
            and str(source).endswith(".previous")
        ):
            raise OSError("injected selector recovery error")
        return replace(source, target)

    def reject_directory(descriptor):
        nonlocal directory_failed
        if (
            when == "directory"
            and not directory_failed
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
        ):
            directory_failed = True
            raise OSError("injected selector directory sync error")
        return fsync(descriptor)

    with (
        patch.object(common, "write_json", side_effect=reject_write),
        patch.object(common.os, "replace", side_effect=reject_restore),
        patch.object(common.os, "fsync", side_effect=reject_directory),
    ):
        with pytest.raises(OSError, match="injected selector"):
            producer("logic_segment_identity", path, generation=1)
    assert previous_store.exists()
    recovery = list(tmp_path.glob("*.previous"))
    if when == "restore":
        assert len(recovery) == 1 and recovery[0].read_bytes() == previous
        assert path.read_bytes() != previous
        assert (tmp_path / json.loads(path.read_text())["artifact"]).is_file()
    else:
        assert path.read_bytes() == previous and not recovery
        assert set(tmp_path.glob("*.sqlite")) == {previous_store}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("changed", ["selector", "store"])
def test_changed_input_during_record_generation_keeps_prior_publication(
    tmp_path, kind, changed
):
    source_path, output = tmp_path / "source.json", tmp_path / "result.json"
    source = producer(kind, source_path)
    with producer(kind, output) as prior:
        old_selector, old_store = output.read_bytes(), prior.store_path.read_bytes()
        old_store_path = prior.store_path
    bindings = source.bound_store_sources()
    changed_path = source.selector_path if changed == "selector" else source.store_path
    module = importlib.import_module("emuflow." + kind + "_storage")
    try:
        with patch.object(module, "publish_sqlite_store") as native:
            with pytest.raises(ValidationError, match="generation changed"):
                producer(
                    kind,
                    output,
                    generation=1,
                    source_bindings=bindings,
                    before_records=lambda: changed_path.chmod(
                        changed_path.stat().st_mode ^ 0o100
                    ),
                )
        native.assert_not_called()
        assert output.read_bytes() == old_selector
        assert old_store_path.read_bytes() == old_store
        assert list(tmp_path.glob("result.json.store.*.sqlite")) == [old_store_path]
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        source.close()


@pytest.mark.parametrize("kind", KINDS)
def test_native_rejection_has_no_reference_fallback(tmp_path, kind):
    output = tmp_path / "result.json"
    with producer(kind, output) as prior:
        old_selector, old_store = output.read_bytes(), prior.store_path.read_bytes()
        old_store_path = prior.store_path
    module = importlib.import_module("emuflow." + kind + "_storage")
    with patch.object(
        module, "publish_sqlite_store", side_effect=ValidationError("native rejected")
    ):
        with pytest.raises(ValidationError, match="native rejected"):
            producer(kind, output, generation=1)
    assert output.read_bytes() == old_selector
    assert old_store_path.read_bytes() == old_store
    assert list(tmp_path.glob("result.json.store.*.sqlite")) == [old_store_path]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", KINDS)
def test_input_scope_is_bound_before_independent_reader_validation(tmp_path, kind):
    output = tmp_path / "result.json"
    with producer(kind, output) as initial:
        view_type, store = type(initial), initial.store_path
    validate = view_type._validate_store

    def changed_during_validation(view):
        validate(view)
        store.chmod(store.stat().st_mode ^ 0o100)

    with patch.object(view_type, "_validate_store", changed_during_validation):
        with pytest.raises(ValidationError, match="generation changed"):
            view_type(output)
    # The failed reader released its lease, and a fresh independent reader may
    # validate the new generation. The old reader never silently rebinds it.
    with view_type(output) as fresh:
        assert fresh.bound_store_sources()
    with pytest.raises(ValidationError, match="closed"):
        fresh.bound_store_sources()


@pytest.mark.parametrize("kind", KINDS)
def test_same_selector_and_hardlink_alias_rejected_before_writer_lease(tmp_path, kind):
    output, alias = tmp_path / "result.json", tmp_path / "alias.json"
    with producer(kind, output) as source:
        old = output.read_bytes()
        os.link(output, alias)
        # The hardlink itself advances ctime; use an independent scope after it.
    with type(source)(output) as source:
        bindings = source.bound_store_sources()
        for target in (output, alias):
            with pytest.raises(ValidationError, match="aliases a source"):
                producer(kind, target, source_bindings=bindings)
        assert output.read_bytes() == old


@pytest.mark.parametrize(
    "kind", [kind for kind in KINDS if kind != "logic_segment_identity"]
)
@pytest.mark.parametrize("when", ["copy", "selector"])
def test_relocation_refuses_input_replacement_and_keeps_prior_generation(
    tmp_path, kind, when
):
    module = importlib.import_module("emuflow." + kind + "_storage")
    output = tmp_path / "destination" / "result.json"
    with producer(kind, output) as prior:
        old_selector, old_store_path = output.read_bytes(), prior.store_path
        old_store = old_store_path.read_bytes()
    source_path = tmp_path / "source.json"
    with producer(kind, source_path, generation=1) as source:
        store = source.store_path
    copy = module.shutil.copyfileobj
    write = module.write_json

    def mutate():
        store.chmod(store.stat().st_mode ^ 0o100)

    def copy_and_change(*args, **kwargs):
        result = copy(*args, **kwargs)
        if when == "copy":
            mutate()
        return result

    def write_and_change(*args, **kwargs):
        result = write(*args, **kwargs)
        if when == "selector":
            mutate()
        return result

    with (
        patch.object(module.shutil, "copyfileobj", copy_and_change),
        patch.object(module, "write_json", write_and_change),
    ):
        with pytest.raises(ValidationError, match="generation changed"):
            getattr(module, "publish_" + kind + "_selector")(
                source_path, output.parent, Path(output.name)
            )
    assert output.read_bytes() == old_selector
    assert old_store_path.read_bytes() == old_store
    assert list(output.parent.glob("result.json.store.*.sqlite")) == [old_store_path]
    assert not list(output.parent.glob("*.tmp"))


@pytest.mark.parametrize("when", ["records", "seal"])
def test_atomic_source_binds_mapped_input_and_source_seal_before_consumption(
    tmp_path, when
):
    from emuflow import xilinx_openparf_source_storage as source_module
    from emuflow.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source
    from emuflow.xilinx_netlist import (
        emit_xilinx_mapped_index,
        open_xilinx_mapped_index,
    )
    from emuflow.yosys import import_yosys_json
    from tests.openparf_runtime_fixture import write_openparf_runtime_fixture

    mapped_json, _, _ = write_openparf_runtime_fixture(tmp_path, include_hard=True)
    ir, mapped, selector = (
        tmp_path / name
        for name in ("mapped.emuir.sqlite", "mapped.sqlite", "source.json")
    )
    import_yosys_json(mapped_json).save(ir)
    report = emit_xilinx_mapped_index(ir, mapped)
    with open_xilinx_mapped_index(mapped) as indexed:
        source = build_xilinx_openparf_atomic_source(
            mapped,
            selector,
            top=report["top"],
            mapped_value=indexed,
            mapped_sha256=report["output_sha256"],
        )
        before, store_path = selector.read_bytes(), source.store_path
        if when == "records":

            def change_mapped():
                mapped.chmod(mapped.stat().st_mode ^ 0o100)
                yield from ()

            with pytest.raises(ValidationError, match="generation changed"):
                source_module.write_xilinx_openparf_atomic_source_storage(
                    selector,
                    mapped_path=mapped,
                    mapped_value=indexed,
                    mapped_sha256=report["output_sha256"],
                    top=report["top"],
                    macro_clusters=change_mapped(),
                    cascades=(),
                    physical_macro_count=0,
                )
        else:
            original_hash = source_module.file_sha256

            def change_after_hash(path):
                value = original_hash(path)
                if Path(path) == store_path:
                    store_path.chmod(store_path.stat().st_mode ^ 0o100)
                return value

            with patch.object(source_module, "file_sha256", change_after_hash):
                with pytest.raises(ValidationError, match="generation changed"):
                    source_module.XilinxOpenparfAtomicSourceView(
                        selector,
                        mapped_path=mapped,
                        mapped_value=indexed,
                        mapped_sha256=report["output_sha256"],
                    )
        assert selector.read_bytes() == before
        assert list(tmp_path.glob("openparf-atomic-source.*.sqlite")) == [store_path]
        assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("changed", ["split-netlists-v3", "emuir-compact-v2"])
def test_full_placement_lowering_refuses_source_change_at_native_publication(changed):
    from emuflow import placement_ir_storage
    from tests.test_phase6 import Phase6Test

    case = Phase6Test()
    case.setUp()
    publish = placement_ir_storage.publish_sqlite_store
    observed = []

    def mutate_then_publish(temporary, destination, schema, *, sources):
        observed.extend(source.schema for source in sources)
        source = next(source for source in sources if source.schema == changed)
        source.path.chmod(source.path.stat().st_mode ^ 0o100)
        try:
            return publish(temporary, destination, schema, sources=sources)
        finally:
            assert not destination.exists()

    with patch.object(
        placement_ir_storage, "publish_sqlite_store", mutate_then_publish
    ):
        with pytest.raises(ValidationError, match="generation changed"):
            case.test_streaming_phase7_lowering_matches_legacy_exactly()
    assert {"split-netlists-v3","emuir-compact-v2","assignment-index-v4","tdm-schedule-store-v4"} <= set(observed)
