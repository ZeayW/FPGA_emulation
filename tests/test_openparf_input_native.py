"""Qualification of the complete source-to-Bookshelf native input owner."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.platform.architecture import ArchitectureDB
from emuflow.physical.openparf.xilinx_openparf_name_map_storage import read_xilinx_openparf_atomic_name_map
from emuflow.common.io import file_sha256
from emuflow.physical.xilinx_netlist import _IndexedCells
from emuflow.physical.openparf import xilinx_openparf_atomic as adapter
from emuflow.physical.openparf.xilinx_openparf_source_storage import (
    XilinxOpenparfAtomicSourceView,
)
from tests.openparf_export_fixture import mapped_facts, export_atomic_facts
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture
from tests import openparf_input_reference as reference


def test_native_input_has_no_host_cell_macro_or_device_population(tmp_path):
    mapped, _, architecture = write_openparf_runtime_fixture(tmp_path)
    # The production typed owner, rather than a materialized JSON view, enters
    # both source construction and the subsequent complete input exporter.
    with mapped_facts(mapped) as owner:
        source_path = owner.path.with_name("atomic-source.json")
        with (
            patch.object(
                _IndexedCells,
                "__iter__",
                side_effect=AssertionError("host cell population"),
            ),
            patch.object(
                _IndexedCells,
                "values",
                side_effect=AssertionError("host cell population"),
            ),
            patch.object(
                XilinxOpenparfAtomicSourceView,
                "_validate_mapped_ownership",
                side_effect=AssertionError("host source rederivation"),
            ),
            patch(
                "emuflow.physical.xilinx_physical_macros.build_xilinx_physical_macro_contract",
                side_effect=AssertionError("host macro population"),
            ),
            patch.object(
                ArchitectureDB,
                "load",
                side_effect=AssertionError("host device population"),
            ),
        ):
            source = adapter.build_xilinx_openparf_atomic_source(
                owner.path, source_path, mapped_value=owner
            )
            manifest = adapter.export_xilinx_openparf_atomic(
                owner.path,
                source_path,
                architecture,
                tmp_path / "input",
                mapped_value=owner,
                packed_value=source,
            )
        assert manifest["status"] == "pass"
        assert manifest["atoms"] == source.selector["counts"]["physical_cells"]
        with closing(sqlite3.connect(tmp_path / "input/site-map.sqlite3")) as db:
            assert (
                db.execute("SELECT count(*) FROM sites").fetchone()[0]
                == manifest["site_database"]["sites"]
            )


def test_native_geometry_matches_independent_small_device_contract(tmp_path):
    mapped, packed, architecture_path = write_openparf_runtime_fixture(
        tmp_path, include_hard=True
    )
    architecture = ArchitectureDB.load(architecture_path)
    sites = reference._placement_sites(architecture, ("DSP48E2", "RAMB36E2", "URAM288"))
    _, _, expected = reference._render_site_geometry(sites)
    manifest = export_atomic_facts(
        mapped, packed, architecture_path, tmp_path / "input"
    )
    with closing(sqlite3.connect(tmp_path / "input/site-map.sqlite3")) as db:
        actual = db.execute(
            "SELECT dense_x,dense_y,physical_x,physical_y,placement_x,placement_y,site FROM sites ORDER BY dense_x,dense_y"
        ).fetchall()
    assert actual == [
        (
            r["dense_x"],
            r["dense_y"],
            r["physical_x"],
            r["physical_y"],
            r["placement_x"],
            r["placement_y"],
            r["site"],
        )
        for r in expected["sites"]
    ]
    assert manifest["clock_region_contract"] == expected["clock_regions"]
    assert manifest["slr_contract"] == expected["super_logic_regions"]
    config = json.loads((tmp_path / "input/openparf.json").read_text())
    assert list(config["gp_model2area_types_map"])[0].startswith("LUT")
    assert list(config["gp_model2area_types_map"])[1].startswith("FD")


def test_source_constants_preserve_global_lexical_singleton_identity(tmp_path):
    mapped, _, architecture = write_openparf_runtime_fixture(tmp_path)
    value = json.loads(mapped.read_text())
    value["modules"]["top"]["cells"]["000_constant"] = {
        "type": "GND",
        "parameters": {},
        "port_directions": {},
        "connections": {},
    }
    mapped.write_text(json.dumps(value))
    with mapped_facts(mapped) as owner:
        selector = owner.path.with_name("atomic-source.json")
        source = adapter.build_xilinx_openparf_atomic_source(
            owner.path, selector, mapped_value=owner
        )
        expected = dict(
            (name, group) for name, group, _ in source.iter_source_assignments()
        )
        out = tmp_path / "input"
        adapter.export_xilinx_openparf_atomic(
            owner.path,
            selector,
            architecture,
            out,
            mapped_value=owner,
            packed_value=source,
        )
        with read_xilinx_openparf_atomic_name_map(
            out / "name_map.json"
        ) as view:
            assert {
                a["instance"]: a["source_cluster"] for a in view["atoms"]
            } == expected


def test_input_republication_failure_preserves_every_prior_member(tmp_path):
    mapped, packed, architecture = write_openparf_runtime_fixture(tmp_path)
    output = tmp_path / "input"
    export_atomic_facts(mapped, packed, architecture, output)
    previous = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
    # Every destination is checked before the first member is replaced.
    (output / "design.scl").unlink()
    (output / "design.scl").symlink_to(mapped)
    with pytest.raises(ValidationError, match="symbolic link"):
        export_atomic_facts(mapped, packed, architecture, output)
    assert mapped.read_bytes() != previous["design.scl"]
    for name, data in previous.items():
        if name != "design.scl":
            assert (output / name).read_bytes() == data
    assert not list(tmp_path.glob(".openparf-input-*"))


def test_native_store_seal_cannot_be_bypassed_by_a_cached_source_view(tmp_path):
    mapped, _, architecture = write_openparf_runtime_fixture(tmp_path)
    with mapped_facts(mapped) as owner:
        selector = owner.path.with_name("atomic-source.json")
        source = adapter.build_xilinx_openparf_atomic_source(
            owner.path, selector, mapped_value=owner
        )
        # Keep a previously checked view while replacing its authoritative
        # relation. Even a direct native consumer must reject the bad seal.
        with closing(sqlite3.connect(source.store_path)) as db, db:
            db.execute(
                "UPDATE metadata SET value='\"wrong\"' WHERE key='logical_sha256'"
            )
        with pytest.raises(ValidationError, match="generation changed"):
            adapter.export_xilinx_openparf_atomic(
                owner.path,
                selector,
                architecture,
                tmp_path / "input",
                mapped_value=owner,
                packed_value=source,
            )
        with pytest.raises(ValidationError, match="seal"):
            adapter.export_xilinx_openparf_atomic(
                owner.path,
                selector,
                architecture,
                tmp_path / "input",
                mapped_value=owner,
            )
        assert not (tmp_path / "input").exists()


def test_retired_input_generators_are_absent_from_installed_modules():
    from emuflow.physical.openparf import xilinx_openparf_source_storage as sources
    from emuflow.physical.openparf import xilinx_openparf_name_map_storage as names

    for name in (
        "_collect_atoms",
        "_IndexedAtomSequence",
        "_OrdinalSafeNames",
        "_device_static_geometry",
        "_placement_sites",
        "_render_site_geometry",
        "_render_library_and_nets",
        "_write_site_database",
    ):
        assert not hasattr(adapter, name)
    assert not hasattr(sources, "write_xilinx_openparf_atomic_source_storage")
    assert not hasattr(names, "write_xilinx_openparf_atomic_name_map_storage")


@pytest.mark.parametrize("changed", ["selector", "store", "mapped"])
def test_native_source_view_cannot_rebind_after_verified_receipt(tmp_path, changed):
    mapped, _, _ = write_openparf_runtime_fixture(tmp_path)
    original = XilinxOpenparfAtomicSourceView._from_native_publication
    with mapped_facts(mapped) as owner:
        selector = owner.path.with_name("atomic-source.json")

        def replace_after_receipt(selector_path, mapped_path, store, **kwargs):
            path = {
                "selector": Path(selector_path),
                "store": Path(store["path"]),
                "mapped": Path(mapped_path),
            }[changed]
            path.chmod(path.stat().st_mode ^ 0o100)
            return original(selector_path, mapped_path, store, **kwargs)

        with (
            patch.object(
                XilinxOpenparfAtomicSourceView,
                "_from_native_publication",
                side_effect=replace_after_receipt,
            ),
            pytest.raises(ValidationError, match="generation changed"),
        ):
            adapter.build_xilinx_openparf_atomic_source(
                owner.path, selector, mapped_value=owner
            )


def test_native_input_rejects_a_different_typed_mapped_namespace(tmp_path):
    import shutil
    from emuflow.physical.xilinx_netlist import open_xilinx_mapped_index

    mapped, _, architecture = write_openparf_runtime_fixture(tmp_path)
    with mapped_facts(mapped) as owner:
        source_path = owner.path.with_name("atomic-source.json")
        source = adapter.build_xilinx_openparf_atomic_source(
            owner.path, source_path, mapped_value=owner
        )
        other = owner.path.with_name("other.sqlite")
        shutil.copyfile(owner.path, other)
        with open_xilinx_mapped_index(other) as other_owner:
            with pytest.raises(ValidationError, match="another artifact"):
                adapter.export_xilinx_openparf_atomic(
                    owner.path,
                    source_path,
                    architecture,
                    tmp_path / "input",
                    mapped_value=other_owner,
                    packed_value=source,
                )
        assert not (tmp_path / "input").exists()


def test_native_input_rejects_a_mixed_top_before_publication(tmp_path):
    mapped, packed, architecture = write_openparf_runtime_fixture(tmp_path)
    source = json.loads(packed.read_text())
    source["top"] = "another-design"
    packed.write_text(json.dumps(source))
    with pytest.raises(ValidationError, match="top disagrees"):
        export_atomic_facts(mapped, packed, architecture, tmp_path / "input")
    assert not (tmp_path / "input").exists()
