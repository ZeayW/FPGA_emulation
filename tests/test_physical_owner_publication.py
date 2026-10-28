"""The remaining physical owners share publication without weakening their checks."""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_store_source

KINDS = ("name_map", "certificate")
MODULES = {
    "name_map": "tests.openparf_names_fixture",
    "certificate": "tests.openparf_placement_reference",
}


def produce(kind, output, generation=0):
    from emuflow.common.io import write_json
    from emuflow.storage.store_publication import bind_store_source

    module = importlib.import_module(MODULES[kind])
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


@pytest.mark.parametrize("kind", KINDS)
def test_native_rejection_preserves_every_prior_owner(tmp_path, kind):
    output = tmp_path / ("mapped.sqlite" if kind == "mapped" else "owner.json")
    store = produce(kind, output)
    old_selector, old_store = output.read_bytes(), store.read_bytes()
    module = importlib.import_module(MODULES[kind])
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
    module = importlib.import_module(MODULES[kind])
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
    module = importlib.import_module(MODULES[kind])
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
    module = importlib.import_module(MODULES[kind])
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


@pytest.fixture
def native_handoff(tmp_path, monkeypatch):
    from tests.native_build import emuflow_native
    from tests.physical_handoff_native_fixture import prepare_native_handoff
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    return prepare_native_handoff(tmp_path/'sources')


def test_native_handoff_rejection_preserves_prior_generation(tmp_path,native_handoff):
    from tests.physical_handoff_native_fixture import build_native_handoff
    from emuflow.physical import handoff as native
    first=build_native_handoff(native_handoff,tmp_path/'first.sqlite')
    store=Path(first['physical_handoff'])
    old=store.read_bytes()
    write=native._write_request
    def rejected(*args,**kwargs):
        result=write(*args,**kwargs)
        request=Path(args[0])
        data=bytearray(request.read_bytes());data[-1]^=1;request.write_bytes(data)
        return result
    with patch.object(native,'_write_request',rejected),pytest.raises(ValidationError):
        build_native_handoff(native_handoff,tmp_path/'rejected.sqlite')
    assert store.read_bytes()==old
    assert not (tmp_path/'rejected.sqlite').exists()
    assert not list(tmp_path.glob('*.tmp'))


def test_native_handoff_original_sources_remain_bound_until_request_execution(tmp_path,native_handoff):
    from tests.physical_handoff_native_fixture import build_native_handoff
    from emuflow.physical import handoff as native
    first=build_native_handoff(native_handoff,tmp_path/'first.sqlite')
    store=Path(first['physical_handoff']);old=store.read_bytes()
    write=native._write_request
    def changed(*args,**kwargs):
        result=write(*args,**kwargs)
        native_handoff.mapped.chmod(native_handoff.mapped.stat().st_mode ^ 0o100)
        return result
    with patch.object(native,'_write_request',changed),pytest.raises(ValidationError,match='generation'):
        build_native_handoff(native_handoff,tmp_path/'changed.sqlite')
    assert store.read_bytes()==old
    assert not (tmp_path/'changed.sqlite').exists()
    assert not list(tmp_path.glob('*.tmp'))


def test_handoff_rejects_active_sql_source_wal_before_new_publication(tmp_path,native_handoff):
    from tests.physical_handoff_native_fixture import build_native_handoff
    first=build_native_handoff(native_handoff,tmp_path/'first.sqlite')
    store=Path(first['physical_handoff']);old=store.read_bytes()
    Path(str(native_handoff.mapped)+'-wal').write_bytes(b'active input writer')
    with pytest.raises(ValidationError):
        build_native_handoff(native_handoff,tmp_path/'active.sqlite')
    assert store.read_bytes()==old
    assert not (tmp_path/'active.sqlite').exists()


@pytest.mark.parametrize('changed', ['mapped', 'certificate', 'output'])
def test_handoff_source_lease_covers_final_lazy_sink_adoption(tmp_path, native_handoff, changed):
    from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract
    from tests.physical_handoff_native_fixture import build_native_handoff
    first = build_native_handoff(native_handoff, tmp_path / 'first')
    prior = Path(first['physical_handoff'])
    original = prior.read_bytes()

    class MutatingSink(dict):
        def update(self, value):
            super().update(value)
            path = (value['clusters']._path if changed == 'output' else
                    getattr(native_handoff, changed))
            path = Path(path)
            path.chmod(path.stat().st_mode ^ 0o100)

    with pytest.raises(ValidationError, match='generation'):
        materialize_xilinx_openparf_atomic_contract(
            native_handoff.mapped, native_handoff.architecture,
            native_handoff.certificate, tmp_path / 'rejected',
            source_packed_path=native_handoff.source, packed_value_sink=MutatingSink())
    assert prior.read_bytes() == original
    assert not list(tmp_path.glob('rejected.generation.*'))


@pytest.mark.parametrize('field', ['name_map_sha256', 'native_placement_sha256'])
def test_handoff_rejects_malformed_certificate_lineage(tmp_path, native_handoff, field):
    from tests.physical_handoff_native_fixture import build_native_handoff
    from tests.openparf_placement_reference import (
        open_xilinx_openparf_atomic_placement,
        write_xilinx_openparf_atomic_placement_certificate,
    )
    with open_xilinx_openparf_atomic_placement(native_handoff.certificate, allow_legacy=False) as view:
        value = dict(view)
        value['clusters'] = list(view.iter_clusters())
    value['source'][field] = 'invalid'
    write_xilinx_openparf_atomic_placement_certificate(native_handoff.certificate, value)
    with pytest.raises(ValidationError, match='lineage digest'):
        build_native_handoff(native_handoff, tmp_path / 'rejected')
    assert not list(tmp_path.glob('rejected.generation.*'))


def test_handoff_rejects_unclaimed_native_device_inputs(tmp_path, native_handoff):
    from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract
    extra = tmp_path / 'unexpected.json'
    extra.write_text('{}')
    with pytest.raises(ValidationError, match='source catalog'):
        materialize_xilinx_openparf_atomic_contract(
            native_handoff.mapped, native_handoff.architecture, native_handoff.certificate,
            tmp_path / 'rejected', source_packed_path=native_handoff.source,
            native_constraints_path=extra, provider_manifest_path=extra)
    assert not list(tmp_path.glob('rejected.generation.*'))


def test_independent_native_audit_rejoins_original_device_semantics(tmp_path, native_handoff):
    import sqlite3
    from emuflow.physical.handoff import audit_physical_handoff
    from emuflow.physical.xilinx_physical_handoff import validate_xilinx_physical_handoff
    from tests.physical_handoff_native_fixture import build_native_handoff
    report = build_native_handoff(native_handoff, tmp_path / 'owner')
    handoff = Path(report['physical_handoff'])
    checked = audit_physical_handoff(native_handoff.mapped, handoff, native_handoff.architecture)
    assert checked['packing']['status'] == checked['placement']['status'] == 'pass'
    with sqlite3.connect(handoff) as db:
        db.execute("UPDATE assignment SET bel='UNKNOWN_BEL' WHERE instance=(SELECT min(instance) FROM assignment)")
    assert validate_xilinx_physical_handoff(handoff)['status'] == 'pass'
    with pytest.raises(ValidationError, match='BEL|bel'):
        audit_physical_handoff(native_handoff.mapped, handoff, native_handoff.architecture)


def test_cli_requires_typed_physical_owner_and_retires_json_validator_routes():
    from emuflow.flow.cli import _build_parser
    parser = _build_parser()
    parsed = parser.parse_args(['arch', 'validate-xilinx-physical', '--mapped-index', 'mapped.sqlite',
                                '--physical-handoff', 'handoff.sqlite', '--architecture', 'architecture.json'])
    assert parsed.arch_command == 'validate-xilinx-physical'
    for retired in ('validate-xilinx-packing', 'validate-xilinx-placement'):
        with pytest.raises(SystemExit):
            parser.parse_args(['arch', retired])
