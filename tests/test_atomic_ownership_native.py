"""Source/certificate ownership only; site legality has separate validators."""

import json
import sqlite3
import subprocess
from collections.abc import Sequence
from contextlib import closing
from unittest.mock import patch

import pytest

from emuflow import physical_cells_native
from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.native_protocol import CheckedRequestWriter
from emuflow.native_tools import native_file_generation
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow.xilinx_openparf_atomic import (
    OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
    open_xilinx_openparf_atomic_placement,
    write_xilinx_openparf_atomic_placement_certificate,
)
from emuflow.xilinx_openparf_source_storage import (
    XilinxOpenparfAtomicSourceView,
    write_xilinx_openparf_atomic_source_storage,
)
from tests.native_build import emuflow_native
from tests.test_rwroute_native import fixture
from tests.test_xilinx_openparf_source_storage import _macro


def prepare(root):
    values = fixture(root)
    mapped = values[-2]
    digest = file_sha256(mapped)
    with open_xilinx_mapped_index(mapped) as indexed:
        source = write_xilinx_openparf_atomic_source_storage(
            root / "source.json",
            mapped_path=mapped,
            mapped_value=indexed,
            mapped_sha256=digest,
            top="top",
            macro_clusters=[_macro("macro-ff", "z_ff")],
            cascades=[],
            physical_macro_count=1,
        )
        # Independent Python reference computes implicit identities, including
        # the mapped rank occupied by the middle-of-order ground constant.
        owners = list(source.iter_source_assignments())
        source.require_sources_unchanged()
    certificate_path = root / "certificate.json"
    certificate = write_xilinx_openparf_atomic_placement_certificate(
        certificate_path,
        {
            "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
            "status": "pass",
            "top": "top",
            "clusters": [
                {
                    "cluster": f"group-{i}",
                    "site": f"SLICE_X0Y{i}",
                    "site_type": "SLICEL",
                    "x": 0,
                    "y": i,
                    "assignments": [
                        {
                            "instance": name,
                            "cell_type": kind,
                            "bel": "AFF",
                            "physical_site": f"SLICE_X0Y{i}",
                            "placement_mode": "fixed",
                            "source_cluster": owner,
                        }
                    ],
                }
                for i, (name, owner, kind) in enumerate(owners)
            ],
        },
    )
    return {
        "mapped": mapped,
        "source": source.store_path,
        "certificate": root / certificate["cluster_storage"]["file"],
        "source_view": source,
        "certificate_path": certificate_path,
        "owners": owners,
        "identities": {
            "mapped_sha256": digest,
            "source_logical_sha256": source.selector["logical_sha256"],
            "certificate_logical_sha256": certificate["cluster_storage"][
                "logical_sha256"
            ],
        },
    }


def validate(root, data):
    with patch.object(
        physical_cells_native,
        "resolve_native_executable",
        return_value=str(emuflow_native()),
    ):
        return physical_cells_native.validate_native_atomic_ownership(
            *(data[name] for name in ("mapped", "source", "certificate")),
            **data["identities"],
            top="top",
            scratch_parent=root,
        )


def test_millionth_singleton_rank_is_streamed_and_includes_constants(tmp_path):
    from emuflow.xilinx_openparf_atomic import _AtomicPlacementCertificateBuilder
    from tests.test_rwroute_native import write_mapped

    # An ownership-only population: a single synthetic certificate group avoids
    # pretending that this fixture is a legal million-cell physical site.
    count = 1_000_002
    mapped = tmp_path / "large.sqlite"
    write_mapped(mapped, {"modules": {"top": {"cells": {}, "ports": {}}}})
    with closing(sqlite3.connect(mapped)) as db, db:
        db.execute(
            "INSERT INTO cells VALUES ('a-constant',0,'GND','{\"type\":\"GND\"}')"
        )
        db.executemany(
            "INSERT INTO cells VALUES (?,?,?,?)",
            ((f"n{i:07d}", i + 1, "LUT1", '{"type":"LUT1"}') for i in range(count)),
        )
    digest = file_sha256(mapped)
    with open_xilinx_mapped_index(mapped) as indexed:
        source = write_xilinx_openparf_atomic_source_storage(
            tmp_path / "large-source.json",
            mapped_path=mapped,
            mapped_value=indexed,
            mapped_sha256=digest,
            top="top",
            macro_clusters=[],
            cascades=[],
            physical_macro_count=0,
        )

        class Assignments(Sequence):
            def __len__(self):
                return count

            def __getitem__(self, key):
                raise AssertionError("random-access ownership materialization")

            def __iter__(self):
                last = None
                for name, owner, kind in source.iter_source_assignments():
                    last = owner
                    yield {
                        "instance": name,
                        "cell_type": kind,
                        "source_cluster": owner,
                        "bel": "A6LUT",
                        "physical_site": "ownership-only",
                        "placement_mode": "fixed",
                    }
                assert last == "atomic-source-1000002"

        selector = tmp_path / "large-certificate.json"
        builder = _AtomicPlacementCertificateBuilder(selector)
        try:
            builder.append(
                {
                    "cluster": "ownership-only",
                    "site": "ownership-only",
                    "site_type": "SLICEL",
                    "x": 0,
                    "y": 0,
                    "assignments": Assignments(),
                }
            )
            certificate = builder.publish(
                {
                    "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                    "status": "pass",
                    "top": "top",
                }
            )
        finally:
            builder.close()
        descriptor = certificate["cluster_storage"]
        data = {
            "mapped": mapped,
            "source": source.store_path,
            "certificate": tmp_path / descriptor["file"],
            "identities": {
                "mapped_sha256": digest,
                "source_logical_sha256": source.selector["logical_sha256"],
                "certificate_logical_sha256": descriptor["logical_sha256"],
            },
        }
        with patch.object(
            XilinxOpenparfAtomicSourceView,
            "iter_source_assignments",
            side_effect=AssertionError("Python million-cell scan"),
        ):
            result = validate(tmp_path, data)
    assert result["cells"] == count + 1 and result["assignments"] == count
    assert result["singleton_clusters"] == count and result["ownership_snapshots"] == 0
    assert result["native_metrics"]["process_lifetime_peak_rss_kib"] < 128 * 1024


def test_ordered_merge_matches_independent_reference_without_python_population(
    tmp_path,
):
    data = prepare(tmp_path)
    owners = {name: owner for name, owner, _kind in data["owners"]}
    assert owners["z_ff"] == "macro-ff"
    assert owners["ram"] == "atomic-source-000007"
    assert owners["中文"] == "atomic-source-000009"
    with (
        patch.object(
            XilinxOpenparfAtomicSourceView,
            "iter_source_assignments",
            side_effect=AssertionError("Python source ownership population"),
        ),
        patch(
            "emuflow.xilinx_netlist._IndexedCells.items",
            side_effect=AssertionError("Python mapped population"),
        ),
    ):
        result = validate(tmp_path, data)
    assert result["cells"] == 10 and result["constants"] == 1
    assert result["assignments"] == 9 and result["macro_assignments"] == 1
    assert result["singleton_clusters"] == 8 and result["ownership_snapshots"] == 0
    assert all(result[key] == value for key, value in data["identities"].items())
    assert not list(tmp_path.glob(".physical-cells-*"))


@pytest.mark.parametrize(
    ("owner", "mutation", "message"),
    [
        (
            "certificate",
            "DELETE FROM assignments WHERE instance='a_lut'",
            "coverage is incomplete",
        ),
        (
            "certificate",
            "UPDATE assignments SET instance='0-extra' WHERE instance='a_lut'",
            "extra or repeated",
        ),
        (
            "certificate",
            "UPDATE assignments SET cell_type='LUT2' WHERE instance='a_lut'",
            "mapped cell identity",
        ),
        (
            "certificate",
            "UPDATE assignments SET source_cluster='wrong' WHERE instance='a_lut'",
            "source cluster identity",
        ),
        (
            "certificate",
            "UPDATE assignments SET instance='ground' WHERE instance='ram'",
            "places a constant",
        ),
        ("source", "DELETE FROM assignments", "macro has no assignments"),
        (
            "source",
            "UPDATE assignments SET instance='aaa-missing'",
            "absent from mapped",
        ),
        ("source", "UPDATE assignments SET cell_type='LUT1'", "ownership or type"),
        (
            "source",
            "UPDATE clusters SET cluster_id='atomic-source-000008'",
            "ownership or type",
        ),
        ("source", "DELETE FROM constants", "constant coverage is incomplete"),
        ("source", "UPDATE constants SET instance='a_lut'", "constant type is invalid"),
        (
            "source",
            "UPDATE metadata SET value=json_set(value,'$.singleton_clusters',999) WHERE key='counts'",
            "coverage disagrees",
        ),
        (
            "source",
            "UPDATE metadata SET value=json_set(value,'$.bytes',0) WHERE key='mapped'",
            "mapped source identity",
        ),
        (
            "source",
            "UPDATE metadata SET value=json_set(value,'$.top','other') WHERE key='header'",
            "mapped source identity",
        ),
        (
            "source",
            "UPDATE metadata SET value='\"other\"' WHERE key='logical_sha256'",
            "logical source identity",
        ),
        (
            "certificate",
            "UPDATE metadata SET value='other' WHERE key='logical_sha256'",
            "logical source identity",
        ),
        ("source", "PRAGMA user_version=99", "storage envelope"),
        (
            "certificate",
            "UPDATE metadata SET value='unknown' WHERE key='schema'",
            "schema",
        ),
    ],
)
def test_rejects_inconsistent_relations(tmp_path, owner, mutation, message):
    data = prepare(tmp_path)
    with closing(sqlite3.connect(data[owner])) as db, db:
        db.execute(mutation)
    before = {
        name: data[name].read_bytes() for name in ("mapped", "source", "certificate")
    }
    with pytest.raises(ValidationError, match=message):
        validate(tmp_path, data)
    assert before == {name: data[name].read_bytes() for name in before}
    assert not list(tmp_path.glob(".physical-cells-*"))


@pytest.mark.parametrize(
    "corruption", ["version", "crc", "truncated", "generation", "source", "alias"]
)
def test_request_failure_preserves_previous_publication(tmp_path, corruption):
    data = prepare(tmp_path)
    request = tmp_path / "request.bin"
    writer = CheckedRequestWriter(request)
    writer.put("QI", 0x314F5341464D5545, 2 if corruption == "version" else 1)
    for name in ("mapped", "source", "certificate"):
        generation = list(native_file_generation(data[name]))
        if name == "source" and corruption == "generation":
            generation[2] += 1
        writer.put("3Q4q", *generation)
    for key in ("mapped_sha256", "source_logical_sha256", "certificate_logical_sha256"):
        writer.text(
            "0" * 64
            if key == "source_logical_sha256" and corruption == "source"
            else data["identities"][key]
        )
    writer.text("top")
    writer.finish()
    writer.close()
    if corruption in {"crc", "truncated"}:
        contents = bytearray(request.read_bytes())
        if corruption == "crc":
            contents[-1] ^= 1
        else:
            contents = contents[:-1]
        request.write_bytes(contents)
    summary = data["source"] if corruption == "alias" else tmp_path / "summary.json"
    if corruption != "alias":
        summary.write_text("prior summary")
    before = summary.read_bytes()
    result = subprocess.run(
        [
            str(emuflow_native()),
            "phase7",
            "validate-atomic-ownership",
            *(str(data[name]) for name in ("mapped", "source", "certificate")),
            str(request),
            str(summary),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert summary.read_bytes() == before


@pytest.mark.parametrize("owner", ["mapped", "source", "selector"])
def test_borrowed_source_view_rejects_changed_generation(tmp_path, owner):
    data = prepare(tmp_path)
    view = data["source_view"]
    path = view.selector_path if owner == "selector" else data[owner]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValidationError, match="changed"):
        view.require_sources_unchanged()


def test_borrowed_certificate_view_rejects_changed_generation(tmp_path):
    data = prepare(tmp_path)
    with open_xilinx_openparf_atomic_placement(
        data["certificate_path"], allow_legacy=False
    ) as view:
        data["certificate"].write_bytes(data["certificate"].read_bytes() + b"\n")
        with pytest.raises(ValidationError, match="changed"):
            view.require_source_unchanged()


@pytest.mark.parametrize("bad_owner", [False, True])
def test_real_bridge_consumes_existing_authorities_without_ownership_mirrors(
    tmp_path, bad_owner
):
    from emuflow.xilinx_openparf_bridge import (
        materialize_xilinx_openparf_atomic_contract,
    )
    from emuflow.xilinx_physical_handoff import XilinxPhysicalHandoffBuilder
    from tests.test_rwroute_native import write_mapped
    from tests.test_xilinx_openparf_bridge import _native_certificate

    mapped_json, architecture, certificate_path = _native_certificate(
        tmp_path, include_hard=False, split_ff_control_sets=True
    )
    mapped = tmp_path / "mapped.sqlite"
    write_mapped(mapped, json.loads(mapped_json.read_text()))
    digest = file_sha256(mapped)
    with open_xilinx_openparf_atomic_placement(
        certificate_path, allow_legacy=False
    ) as view:
        certificate = dict(view)
        certificate["clusters"] = list(view.iter_clusters())
    with open_xilinx_mapped_index(mapped) as indexed:
        source_path = tmp_path / "typed-source.json"
        source = write_xilinx_openparf_atomic_source_storage(
            source_path,
            mapped_path=mapped,
            mapped_value=indexed,
            mapped_sha256=digest,
            top="top",
            macro_clusters=[],
            cascades=[],
            physical_macro_count=0,
        )
        owners = {name: owner for name, owner, _ in source.iter_source_assignments()}
        certificate["source"]["mapped_sha256"] = digest
        certificate["source"]["packed_sha256"] = file_sha256(source_path)
        for cluster in certificate["clusters"]:
            for assignment in cluster["assignments"]:
                assignment["source_cluster"] = owners[assignment["instance"]]
        if bad_owner:
            certificate["clusters"][0]["assignments"][0][
                "source_cluster"
            ] = "wrong-owner"
        write_xilinx_openparf_atomic_placement_certificate(
            certificate_path, certificate
        )
        output = tmp_path / "physical.sqlite"
        if bad_owner:
            output.write_text("previous publication")
        else:
            # Explicit small-fixture oracle: the former ownership snapshot
            # comparison remains useful here, but is never a native fallback.
            with patch.object(
                physical_cells_native,
                "resolve_native_executable",
                return_value=str(emuflow_native()),
            ):
                materialize_xilinx_openparf_atomic_contract(
                    mapped,
                    architecture,
                    certificate_path,
                    tmp_path / "reference.sqlite",
                    mapped_value=indexed,
                    source_packed_path=source_path,
                    source_packed_value=source.materialize(),
                )
        with (
            patch.object(
                physical_cells_native,
                "resolve_native_executable",
                return_value=str(emuflow_native()),
            ),
            patch.object(
                XilinxPhysicalHandoffBuilder,
                "append_source_assignment",
                side_effect=AssertionError("source ownership mirror"),
            ),
            patch.object(
                XilinxPhysicalHandoffBuilder,
                "validate_source_ownership",
                side_effect=AssertionError("second ownership join"),
            ),
            patch.object(
                XilinxOpenparfAtomicSourceView,
                "iter_source_assignments",
                side_effect=AssertionError("Python ownership population"),
            ),
            patch.object(
                type(indexed.cells),
                "items",
                side_effect=AssertionError("whole Python cell scan"),
            ),
        ):

            def materialize():
                return materialize_xilinx_openparf_atomic_contract(
                    mapped,
                    architecture,
                    certificate_path,
                    output,
                    mapped_value=indexed,
                    source_packed_path=source_path,
                    source_packed_value=source,
                )

            if bad_owner:
                with pytest.raises(ValidationError, match="source cluster identity"):
                    materialize()
                assert output.read_text() == "previous publication"
                return
            result = materialize()
    receipt = result["validation"]["source_ownership"]
    assert result["status"] == "pass"
    assert receipt["assignments"] == len(owners) == result["placed_cells"]
    assert receipt["ownership_snapshots"] == 0
    assert output.read_bytes() == (tmp_path / "reference.sqlite").read_bytes()
    assert (
        result["runtime_breakdown"]["dataflow"]["certificate_assignment_ownership"]
        == "native-source-certificate-ordered-merge-v1"
    )
