"""The remaining physical owners share publication without weakening their checks."""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.store_publication import bind_store_source

KINDS = ("mapped", "name_map", "certificate", "handoff")
MODULES = {
    "mapped": "xilinx_netlist",
    "name_map": "xilinx_openparf_name_map_storage",
    "certificate": "xilinx_openparf_atomic",
    "handoff": "xilinx_physical_handoff",
}


def produce(kind, output, generation=0):
    from emuflow.io import write_json
    from emuflow.store_publication import bind_store_source

    module = importlib.import_module("emuflow." + MODULES[kind])
    if kind == "mapped":
        from emuflow.yosys import import_yosys_json
        from tests.test_xilinx_netlist import XilinxMappedNetlistTests

        value = XilinxMappedNetlistTests._fixture()
        value["modules"]["top"]["cells"]["lut"]["parameters"]["INIT"] = (
            "10" if generation == 0 else "01"
        )
        raw, ir = output.with_suffix(".input.json"), output.with_suffix(
            ".input.emuir.sqlite"
        )
        write_json(raw, value)
        import_yosys_json(raw).save(ir)
        module.emit_xilinx_mapped_index(ir, output)
        return output
    upstream = output.parent / "input.txt"
    upstream.write_text("immutable upstream")
    sources = (bind_store_source(upstream, "physical-input-document-v1"),)
    if kind == "name_map":
        from tests.test_xilinx_openparf_name_map_storage import (
            _header,
            _atoms,
            _DIGESTS,
        )

        with module.write_xilinx_openparf_atomic_name_map_storage(
            output,
            header=_header(),
            atoms=_atoms(str(generation)),
            carry_cascade_chains=(),
            source_bindings=sources,
            **_DIGESTS,
        ) as view:
            return view.store_path
    if kind == "certificate":
        value = module.write_xilinx_openparf_atomic_placement_certificate(
            output,
            {
                "schema": module.OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                "clusters": [
                    {
                        "cluster": "openparf:SLICE_X0Y0",
                        "site": "SLICE_X0Y0",
                        "site_type": "SLICEL",
                        "x": 0,
                        "y": 0,
                        "assignments": [
                            {
                                "instance": f"u{generation}",
                                "cell_type": "LUT6",
                                "bel": "A6LUT",
                                "physical_site": "SLICE_X0Y0",
                                "placement_mode": "SLICEL",
                                "source_cluster": f"atomic-source:u{generation}",
                                "bram_tile_group": None,
                            }
                        ],
                    }
                ],
            },
            source_bindings=sources,
        )
        return output.parent / value["cluster_storage"]["file"]
    from tests.test_xilinx_physical_handoff import _contracts, XilinxPhysicalHandoffTest

    packed, placement = _contracts()
    with module.XilinxPhysicalHandoffBuilder(output) as builder:
        builder.bind_store_sources(sources)
        builder.append_bridge_cluster(
            XilinxPhysicalHandoffTest._bridge_cluster(generation)
        )
        builder.seal(packed, placement, cascades=(), constants=())
        module.validate_xilinx_physical_handoff(builder.temporary_path)
        builder.publish()
    return output


@pytest.mark.parametrize("kind", KINDS)
def test_native_rejection_preserves_every_prior_owner(tmp_path, kind):
    output = tmp_path / ("mapped.sqlite" if kind == "mapped" else "owner.json")
    store = produce(kind, output)
    old_selector, old_store = output.read_bytes(), store.read_bytes()
    module = importlib.import_module("emuflow." + MODULES[kind])
    with patch.object(
        module, "publish_sqlite_store", side_effect=ValidationError("native rejected")
    ) as native:
        with pytest.raises(ValidationError, match="native rejected"):
            produce(kind, output, generation=1)
    assert native.call_count == 1
    assert output.read_bytes() == old_selector and store.read_bytes() == old_store
    assert not list(tmp_path.glob("*.tmp"))
    if kind in ("name_map", "certificate"):
        assert [p for p in tmp_path.glob("*.sqlite") if p != store] == []


@pytest.mark.parametrize("kind", KINDS)
def test_original_input_scope_survives_until_native_adoption(tmp_path, kind):
    output = tmp_path / ("mapped.sqlite" if kind == "mapped" else "owner.json")
    store = produce(kind, output)
    old_selector, old_store = output.read_bytes(), store.read_bytes()
    module = importlib.import_module("emuflow." + MODULES[kind])
    publish = module.publish_sqlite_store
    observed = []

    def change_after_semantic_checks(temporary, destination, schema, *, sources):
        observed.extend(sources)
        source = sources[0]
        source.path.chmod(source.path.stat().st_mode ^ 0o100)
        return publish(temporary, destination, schema, sources=sources)

    with patch.object(module, "publish_sqlite_store", change_after_semantic_checks):
        with pytest.raises(ValidationError, match="generation changed"):
            produce(kind, output, generation=1)
    assert observed
    assert output.read_bytes() == old_selector and store.read_bytes() == old_store
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["name_map", "certificate"])
def test_input_changes_before_selector_commit_remove_only_uncommitted_sql(
    tmp_path, kind
):
    output = tmp_path / "owner.json"
    store = produce(kind, output)
    old_selector, old_store = output.read_bytes(), store.read_bytes()
    module = importlib.import_module("emuflow." + MODULES[kind])
    write = module.write_json

    def change_at_commit(*args, **kwargs):
        result = write(*args, **kwargs)
        source = tmp_path / "input.txt"
        source.chmod(source.stat().st_mode ^ 0o100)
        return result

    with patch.object(module, "write_json", change_at_commit):
        with pytest.raises(ValidationError, match="generation changed"):
            produce(kind, output, generation=1)
    assert output.read_bytes() == old_selector and store.read_bytes() == old_store
    assert list(tmp_path.glob("*.sqlite")) == [store]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["name_map", "certificate"])
@pytest.mark.parametrize("changed", ["selector", "store"])
def test_reader_cannot_rebind_the_generation_after_validation(tmp_path, kind, changed):
    output = tmp_path / "owner.json"
    produce(kind, output)
    module = importlib.import_module("emuflow." + MODULES[kind])
    open_view = (
        module.XilinxOpenparfAtomicNameMapView
        if kind == "name_map"
        else module.open_xilinx_openparf_atomic_placement
    )
    with open_view(output) as view:
        original = view.bound_store_sources()
        path = output if changed == "selector" else view.store_path
        path.chmod(path.stat().st_mode ^ 0o100)
        with pytest.raises(ValidationError, match="generation changed"):
            view.bound_store_sources()
        expected = (
            {"openparf-name-map-selector-v1", "openparf-name-map-store-v1"}
            if kind == "name_map"
            else {"openparf-placement-selector-v2", "openparf-placement-certificate-v3"}
        )
        assert {source.schema for source in original} == expected
    with pytest.raises(ValidationError, match="closed"):
        view.bound_store_sources()


def test_handoff_rejects_active_sql_source_wal_after_independent_validation(tmp_path):
    from emuflow.xilinx_physical_handoff import XilinxPhysicalHandoffBuilder
    from tests.test_xilinx_physical_handoff import _contracts, XilinxPhysicalHandoffTest

    mapped = tmp_path / "mapped.sqlite"
    produce("mapped", mapped)
    source = bind_store_source(mapped, "xilinx-mapped-index-v2")
    output = tmp_path / "handoff.sqlite"
    packed, placement = _contracts()
    with XilinxPhysicalHandoffBuilder(output) as builder:
        builder.bind_store_sources((source,))
        builder.append_bridge_cluster(XilinxPhysicalHandoffTest._bridge_cluster(0))
        builder.seal(packed, placement, cascades=(), constants=())
        Path(str(mapped) + "-wal").write_bytes(b"active input writer")
        with pytest.raises(ValidationError, match="native SQLite publication failed"):
            builder.publish()
        assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))
