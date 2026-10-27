"""Source-bound publication failures must preserve the last physical evidence."""

import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from tests.test_split_overlay_native import owners
from tests.logic_identity_fixture import identity_facts


KINDS = (
    "boundary_identity",
    "boundary_timing",
    "logic_segment_identity",
    "logic_segment_timing",
    "local_path_timing",
)


PYTHON_WRITER_KINDS = ("boundary_identity",)


def producer(
    kind, path, *, generation=0, source_bindings=(), before_records=None, **options
):
    module = importlib.import_module("emuflow.timing." + kind + "_storage")
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

    if kind == "logic_segment_identity":
        assert not source_bindings and before_records is None and not options
        return identity_facts(path, header=header, segments=records)
    from tests.timing_facts import physical_facts_writer
    writer = physical_facts_writer(kind)
    if kind == "logic_segment_timing":
        assert not source_bindings and before_records is None and not options
        return writer(path, header=header, segments=records)
    if kind in {"local_path_timing", "boundary_timing"}:
        assert not source_bindings and before_records is None and not options
        return writer(path, header, records)
    if kind.startswith("logic_"):
        return writer(
            path,
            header=header,
            segments=stream(),
            source_bindings=source_bindings,
            **options,
        )
    return writer(path, header, stream(), source_bindings=source_bindings)


@pytest.mark.parametrize("kind", PYTHON_WRITER_KINDS)
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
    module = importlib.import_module("emuflow.timing." + kind + "_storage")
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


@pytest.mark.parametrize("kind", PYTHON_WRITER_KINDS)
def test_native_rejection_has_no_reference_fallback(tmp_path, kind):
    output = tmp_path / "result.json"
    with producer(kind, output) as prior:
        old_selector, old_store = output.read_bytes(), prior.store_path.read_bytes()
        old_store_path = prior.store_path
    module = importlib.import_module("emuflow.timing." + kind + "_storage")
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


@pytest.mark.parametrize("kind", PYTHON_WRITER_KINDS)
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
    module = importlib.import_module("emuflow.timing." + kind + "_storage")
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
    from emuflow.physical.openparf import xilinx_openparf_source_storage as source_module
    from emuflow.physical.openparf.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source
    from emuflow.physical.xilinx_netlist import (
        emit_xilinx_mapped_index,
        open_xilinx_mapped_index,
    )
    from emuflow.frontend.yosys import import_yosys_json
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
def test_full_placement_lowering_refuses_changed_original_sources(changed, owners, tmp_path):
    from emuflow.physical import placement_lowering_native
    from emuflow.transport.split_overlay import bind_split_sources
    from tests.test_placement_lowering_native import prepare
    from emuflow.physical.lowering import run_placement_ir_lowering_from_split

    case, split, fpga, transport, logical, ir, source = prepare(owners, tmp_path)
    output = tmp_path / "native/placement.emuir.sqlite"
    launch = placement_lowering_native.NativeToolProcess
    observed = []

    def mutate_then_launch(tool,command,**kwargs):
        request=json.loads(Path(command[-2]).read_text())
        sources=bind_split_sources(Path(request['sources'][0]['path']))
        observed.extend(source.schema for source in sources)
        source = next(source for source in sources if source.schema == changed)
        source.path.chmod(source.path.stat().st_mode ^ 0o100)
        return launch(tool,command,**kwargs)

    with patch.object(
        placement_lowering_native, "NativeToolProcess", mutate_then_launch
    ):
        with pytest.raises(ValidationError, match="generation changed"):
            run_placement_ir_lowering_from_split(split, fpga, transport.manifest_path, source, output)
    assert not output.parent.exists()
    assert not list(tmp_path.glob(".lowering-stage-*"))
    assert {"split-netlists-v3","emuir-compact-v2","assignment-index-v4","tdm-schedule-store-v4"} <= set(observed)
