"""Cross-phase publication failures must not invalidate prior primary owners."""

import errno
import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_store_source

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("emuir", "paths", "weights", "qor", "functions", "sites")


def coordinates(generation=0):
    name = f"SLICE_X{generation + 1}Y2"
    return {
        "sites": [
            {
                "dense_x": 0,
                "dense_y": 0,
                "physical_x": generation + 1,
                "physical_y": 2,
                "placement_x": 0.0,
                "placement_y": 0.0,
                "site": name,
                "resources": {"LUT": 8, "FF": 16},
                "physical_sites": {"LUT": [name], "FF": [name]},
            }
        ]
    }


def setup_case(kind, root):
    from emuflow.frontend.yosys import import_yosys_json
    from emuflow.timing import sta

    ir = import_yosys_json(
        ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
    )
    ir_path = root / "input.emuir.sqlite"
    ir.save(ir_path)
    input_text = root / "input.txt"
    input_text.write_text("immutable input")
    sources = (bind_store_source(input_text, "sta-source-document-v1"),)
    output = root / ("result.emuir.sqlite" if kind == "emuir" else "result.json")
    module_name = {
        "emuir": "emuflow.ir.emuir_compact",
        "routes": "emuflow.routing.system_route_storage",
        "paths": "emuflow.timing.sta",
        "weights": "emuflow.timing.sta",
        "qor": "emuflow.timing.qor_storage",
        "functions": "emuflow.physical.vivado.vivado_indexed_netlist",
        "sites": "emuflow.physical.openparf.xilinx_openparf_atomic",
    }[kind]
    module = importlib.import_module(module_name)
    measured = root / "paths.tsv"

    def write_measurement(generation=0):
        measured.write_text(
            sta.STA_PATH_DATABASE_TSV_HEADER
            + "\n"
            + "\t".join(
                [
                    f"path{generation}".encode().hex(),
                    "clk".encode().hex(),
                    "10",
                    "-2",
                    "1",
                    ir.value["nets"][0]["id"].encode().hex(),
                ]
            )
            + "\n"
        )

    if kind == "weights":
        write_measurement()
        sta.import_sta_path_database_tsv_streaming(
            measured,
            ir_path,
            root / "paths.json",
            provider="opensta-fpga-path-database-v1",
        )
    if kind == "functions":
        from tests.test_vivado_indexed_physical import fixture

        ir_path = fixture(root)
    def produce(generation=0):
        if kind == "emuir":
            value = ir.to_dict()
            value["design"] = {**value["design"], "name": f"design{generation}"}
            module.write_compact_emuir(output, value, source_bindings=sources)
            return output
        if kind == "routes":
            from tests.test_phase4 import _storage_routes

            from tests.native_route_seal_fixture import write_route_store_fixture
            manifest = write_route_store_fixture(output, _storage_routes(generation))
            return root / manifest["artifact"]
        if kind == "paths":
            write_measurement(generation)
            module.import_sta_path_database_tsv_streaming(
                measured, ir_path, output, provider="opensta-fpga-path-database-v1"
            )
            return root / json.loads(output.read_text())["payloads"]["paths"]["path"]
        if kind == "weights":
            module.derive_partition_net_weights(
                root / "paths.json", ir_path, output, criticality_scale=9.0 + generation
            )
            return root / json.loads(output.read_text())["payloads"]["weights"]["path"]
        if kind == "qor":
            from tests.test_qor_storage import _report

            value = _report()
            value["design"] = f"design{generation}"
            with module.write_qor_report(
                output, value, source_bindings=sources
            ) as view:
                return view.store_path
        if kind == "functions":
            module.emit_indexed_vivado_verilog(ir_path, root / "design.v")
            return root / "design.functions.sqlite"
        module._write_site_database(
            output, coordinates(generation), source_bindings=sources
        )
        return output

    if kind == "functions":
        output = root / "design.functions.sqlite"
    return module, output, produce


@pytest.mark.parametrize("kind", KINDS)
def test_native_rejection_keeps_prior_primary_owner(
    tmp_path, kind
):
    module, output, produce = setup_case(kind, tmp_path)
    previous = produce()
    old = (output.read_bytes(), previous.read_bytes())
    with patch.object(
        module, "publish_derived_outputs" if kind == "functions" else "publish_sqlite_store",
        side_effect=ValidationError("native rejected")
    ) as native:
        with pytest.raises(ValidationError, match="native rejected"):
            produce(1)
    native.assert_called_once()
    assert output.read_bytes() == old[0] and previous.read_bytes() == old[1]
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.previous"))


@pytest.mark.parametrize("kind", KINDS)
def test_changed_source_after_row_build_rejects_publication(tmp_path, kind):
    module, output, produce = setup_case(kind, tmp_path)
    previous = produce()
    old = (output.read_bytes(), previous.read_bytes())
    if kind == "functions":
        publish = module.publish_derived_outputs

        def mutate_then_group(artifacts, *, sources):
            assert sources
            source = sources[0]
            source.path.chmod(source.path.stat().st_mode ^ 0o100)
            return publish(artifacts, sources=sources)

        with patch.object(module, "publish_derived_outputs", mutate_then_group):
            with pytest.raises(ValidationError, match="generation changed"):
                produce(1)
        assert output.read_bytes() == old[0] and previous.read_bytes() == old[1]
        assert not list(tmp_path.glob("*.tmp"))
        return
    publish = module.publish_sqlite_store

    def mutate_then_adopt(temporary, destination, schema, *, sources):
        assert sources
        source = sources[0]
        source.path.chmod(source.path.stat().st_mode ^ 0o100)
        return publish(temporary, destination, schema, sources=sources)

    with patch.object(module, "publish_sqlite_store", mutate_then_adopt):
        with pytest.raises(ValidationError, match="generation changed"):
            produce(1)
    assert output.read_bytes() == old[0] and previous.read_bytes() == old[1]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["paths", "weights"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_sta_selector_failure_preserves_old_payload_and_retires_uncommitted_generation(
    tmp_path, kind, when
):
    module, output, produce = setup_case(kind, tmp_path)
    previous = produce()
    old = (output.read_bytes(), previous.read_bytes())
    from emuflow.storage import store_publication

    write = store_publication.write_json

    def reject_commit(path, *args, **kwargs):
        if path == output:
            if when == "after":
                write(path, *args, **kwargs)
            raise OSError("selector rejected")
        return write(path, *args, **kwargs)

    with patch.object(store_publication, "write_json", reject_commit):
        with pytest.raises(OSError, match="selector rejected"):
            produce(1)
    assert output.read_bytes() == old[0] and previous.read_bytes() == old[1]
    assert list(tmp_path.glob(f"{output.name}.{kind}.*.sqlite")) == [previous]
    assert not list(tmp_path.glob("*.previous"))




def test_site_alias_failure_keeps_prior_target_and_shares_successful_bytes(tmp_path):
    from emuflow.physical.openparf import xilinx_openparf_atomic as sites
    from emuflow.common.io import file_sha256
    from emuflow.common.native_tools import native_file_generation

    source, target = tmp_path / "source.sqlite", tmp_path / "alias.sqlite"
    sites._write_site_database(source, coordinates())
    target.write_bytes(b"previous target")
    state = {
        "site_database": source,
        "site_database_generation": native_file_generation(source),
        "site_database_sha256": file_sha256(source),
        "site_count": 1,
        "coordinate_system": coordinates(),
    }
    replace = os.replace

    def fail_alias(src, dst):
        if Path(dst) == target:
            raise OSError(errno.EIO, "alias rejected")
        return replace(src, dst)

    with patch.object(sites.os, "replace", fail_alias):
        with pytest.raises(OSError, match="alias rejected"):
            sites._materialize_site_database(target, state)
    assert target.read_bytes() == b"previous target"
    assert not list(tmp_path.glob("*.tmp"))
    sites._materialize_site_database(target, state)
    assert os.path.samefile(source, target)
    assert source.read_bytes() == target.read_bytes()


@pytest.mark.parametrize("kind", ["paths", "weights"])
def test_incomplete_selector_backup_never_replaces_a_valid_previous_selector(
    tmp_path, kind
):
    module, output, produce = setup_case(kind, tmp_path)
    previous = produce()
    old = (output.read_bytes(), previous.read_bytes())

    def fail_copy(source, target, *args, **kwargs):
        Path(target).write_bytes(b"partial backup")
        raise OSError("backup rejected")

    from emuflow.storage import store_publication

    with patch.object(store_publication.shutil, "copyfile", fail_copy):
        with pytest.raises(OSError, match="backup rejected"):
            produce(1)
    assert output.read_bytes() == old[0] and previous.read_bytes() == old[1]
    assert not list(tmp_path.glob("*.previous"))


@pytest.mark.parametrize("kind", ["routes", "qor"])
@pytest.mark.parametrize("changed", ["selector", "store"])
def test_route_and_qor_readers_preserve_the_generation_they_validated(
    tmp_path, kind, changed
):
    module, output, produce = setup_case(kind, tmp_path)
    produce()
    view = (
        module.SystemRoutesView(output)
        if kind == "routes"
        else module.QorReportView(output)
    )
    try:
        before = view.bound_store_sources()
        path = output if changed == "selector" else view.store_path
        path.chmod(path.stat().st_mode ^ 0o100)
        with pytest.raises(ValidationError, match="generation changed"):
            view.bound_store_sources()
        assert len(before) == 2
    finally:
        close = getattr(view, "close", None)
        if callable(close):
            close()




def test_schedule_cannot_recapture_a_changed_validated_route_owner(
    tmp_path, monkeypatch
):
    from emuflow.scheduling.tdm import build_native_exact_schedule_request
    from tests.native_schedule_case import _case
    from tests.test_native_schedule_generation import install_runtime

    case, routes = _case(tmp_path, False, False)
    install_runtime(tmp_path, monkeypatch)
    routes.store_path.chmod(routes.store_path.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match="generation changed"):
        build_native_exact_schedule_request(routes, case.platform)
