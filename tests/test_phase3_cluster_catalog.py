"""Final production records use primary columns, not expanded host clusters."""

from contextlib import closing
from pathlib import Path
import json
import sqlite3
import struct
from unittest.mock import patch
import zlib

import pytest

from emuflow import phase3_storage as storage, store_publication
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.native_tools import native_file_generation
from emuflow.partition import build_clusters, CUT_MODE_STATIC_EXACT
from tests.test_phase3_clusters_native import source


def catalogs(source, mode="static"):
    ir, constraints = source
    if mode in {"positive", "inline-bitmap"}:
        from tests.test_combinational_cut import _chain_ir
        from emuflow.emuir_compact import write_compact_emuir
        from emuflow.partition_ir import load_partition_ir

        value = _chain_ir().to_dict()
        for net in value["nets"]:
            net["id"] += "雪"
        write_compact_emuir(ir.path, value)
        ir = load_partition_ir(ir.path)
    if mode == "groups":
        constraints = dict(
            constraints, groups=[{"id": "雪-group", "instances": ["u0", "u3"]}]
        )
    if mode == "fixed":
        constraints = dict(constraints, fixed=[{"instance": "u0", "fpga": "a"}])
    if mode in {"unicode", "resource-max", "sparse"}:
        with closing(sqlite3.connect(ir.path)) as connection, connection:
            if mode == "unicode":
                connection.execute("UPDATE cell_records SET id='é雪' WHERE seq=0")
            elif mode == "resource-max":
                row = connection.execute(
                    "SELECT resources_seq FROM cell_records WHERE seq=0"
                ).fetchone()
                connection.execute(
                    "UPDATE payloads SET value=? WHERE seq=?",
                    (json.dumps({"lut": 4294967295}), row[0]),
                )
            else:
                connection.execute("UPDATE endpoint_records SET ordinal=ordinal*7+3")
    kwargs = {
        "cut_mode": "sequential-only" if mode == "normal" else CUT_MODE_STATIC_EXACT,
        "_stream_eligible_policy_names": True,
    }
    expected = build_clusters(ir, constraints, **kwargs)
    actual = build_clusters(ir, constraints, _native_compact_clusters=True, **kwargs)
    if mode in {"positive", "inline-bitmap"}:
        assert len(actual["policy"]["eligible_combinational_cut_nets"]) > 0
    if mode == "inline-bitmap":
        actual["policy"]["eligible_combinational_cut_nets"] = expected["policy"][
            "eligible_combinational_cut_nets"
        ]
    if mode == "plain-names":
        for value in (actual, expected):
            value["policy"]["eligible_combinational_cut_nets"] = list(
                reversed(value["policy"]["eligible_combinational_cut_nets"])
            )
    return actual, expected


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "static",
        "groups",
        "fixed",
        "unicode",
        "resource-max",
        "sparse",
        "plain-names",
        "positive",
        "inline-bitmap",
    ],
)
def test_native_final_sections_match_reference_without_python_record_or_name_expansion(
    source, tmp_path, mode
):
    actual, expected = catalogs(source, mode)
    storage.write_indexed_phase3_clusters(
        tmp_path / "reference/clusters.json", expected
    )
    rows = actual["clusters"]
    eligible = actual["policy"].get("eligible_combinational_cut_nets")
    with (
        patch.object(
            type(rows), "_record", side_effect=AssertionError("host cluster expansion")
        ),
        patch.object(
            storage,
            "_build_reference_cluster_store",
            side_effect=AssertionError("reference retry"),
        ),
    ):
        if getattr(eligible, "_source", None) is not None:
            with patch.object(
                type(eligible),
                "__iter__",
                side_effect=AssertionError("host candidate names"),
            ):
                result = storage.write_indexed_phase3_clusters(
                    tmp_path / "native/clusters.json", actual
                )
        else:
            result = storage.write_indexed_phase3_clusters(
                tmp_path / "native/clusters.json", actual
            )
    assert result["counts"]["instances"] == actual["instances"]
    assert read_json(tmp_path / "native/clusters.json") == read_json(
        tmp_path / "reference/clusters.json"
    )
    # Compare every logical column record, independently of SQLite page layout
    # and compressor call boundaries. No sampled cluster/member check.
    stores = []
    for arm in ("native", "reference"):
        selector = json.loads((tmp_path / arm / "clusters.json").read_text())
        with closing(
            sqlite3.connect(tmp_path / arm / selector["artifact"])
        ) as connection:
            sections = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT section,records,raw_bytes FROM sections ORDER BY section"
                )
            }
            payloads = {
                name: zlib.decompress(
                    b"".join(
                        row[0]
                        for row in connection.execute(
                            "SELECT payload FROM section_chunks WHERE section=? ORDER BY chunk_seq",
                            (name,),
                        )
                    )
                )
                for name in sections
            }
            stores.append((sections, payloads))
    assert stores[0] == stores[1]


@pytest.mark.parametrize(
    "failure",
    [
        "native",
        "source",
        "primary",
        "candidate",
        "selector-before",
        "selector-after",
        "backup",
    ],
)
def test_failed_catalog_adoption_or_selector_keeps_previous_generation(
    source, tmp_path, failure
):
    actual, expected = catalogs(source)
    output = tmp_path / "clusters.json"
    previous = storage.write_indexed_phase3_clusters(
        output, {**expected, "design": "previous"}
    )
    payload = tmp_path / previous["artifact"]
    before = output.read_bytes(), payload.read_bytes()
    publish, write, copy = (
        storage.publish_sqlite_store,
        store_publication.write_json,
        store_publication.shutil.copyfile,
    )

    def adopt(*args, **kwargs):
        if failure == "native":
            raise ValidationError("native rejected")
        if failure in {"source", "primary", "candidate"}:
            assert len(kwargs["sources"]) == 3
            changed = kwargs["sources"][
                {"source": 0, "primary": 1, "candidate": 2}[failure]
            ].path
            changed.chmod(changed.stat().st_mode ^ 0o100)
        return publish(*args, **kwargs)

    def commit(path, *args, **kwargs):
        if failure == "selector-before":
            raise OSError("selector rejected")
        result = write(path, *args, **kwargs)
        if failure == "selector-after":
            raise OSError("selector rejected")
        return result

    def backup(*args):
        if failure == "backup":
            Path(args[1]).write_bytes(b"partial")
            raise OSError("backup rejected")
        return copy(*args)

    with (
        patch.object(storage, "publish_sqlite_store", side_effect=adopt),
        patch.object(store_publication, "write_json", side_effect=commit),
        patch.object(store_publication.shutil, "copyfile", side_effect=backup),
        patch.object(
            storage,
            "_build_reference_cluster_store",
            side_effect=AssertionError("reference retry"),
        ),
    ):
        with pytest.raises(
            (ValidationError, OSError), match="rejected|generation changed"
        ):
            storage.write_indexed_phase3_clusters(output, actual)
    assert output.read_bytes() == before[0] and payload.read_bytes() == before[1]
    assert list(tmp_path.glob("clusters.*.sqlite")) == [payload]
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.previous"))


@pytest.mark.parametrize("corruption", ["duplicate-member", "name"])
def test_resealed_primary_semantic_corruption_cannot_publish_catalog(
    source, tmp_path, corruption
):
    actual, expected = catalogs(source, "groups")
    output = tmp_path / "clusters.json"
    previous = storage.write_indexed_phase3_clusters(output, expected)
    before = output.read_bytes(), (tmp_path / previous["artifact"]).read_bytes()
    primary = actual["clusters"]
    path = primary.native_artifact
    data = bytearray(path.read_bytes())
    cells, clusters = struct.unpack_from("<QQ", data, 12)
    offsets = 200 + 8 * (cells + 1)
    member_rows = offsets + 4 * (clusters + 1)
    if corruption == "duplicate-member":
        bounds = struct.unpack_from("<" + "I" * (clusters + 1), data, offsets)
        cluster = next(i for i in range(clusters) if bounds[i + 1] - bounds[i] >= 2)
        first = struct.unpack_from("<I", data, member_rows + 4 * bounds[cluster])[0]
        struct.pack_into("<I", data, member_rows + 4 * (bounds[cluster] + 1), first)
    else:
        names = member_rows + 8 * cells + 48 * clusters
        data[names] = ord("z")
    struct.pack_into("<I", data, len(data) - 4, zlib.crc32(data[:-4]))
    path.write_bytes(data)
    primary.native_generation = native_file_generation(path)
    with patch.object(
        storage,
        "_build_reference_cluster_store",
        side_effect=AssertionError("reference retry"),
    ):
        with pytest.raises(ValidationError, match="native cluster publication failed"):
            storage.write_indexed_phase3_clusters(output, actual)
    assert output.read_bytes() == before[0]
    assert (tmp_path / previous["artifact"]).read_bytes() == before[1]


def test_historical_fixed_name_catalog_remains_readable(source, tmp_path):
    _actual, expected = catalogs(source)
    selector = storage.write_indexed_phase3_clusters(
        tmp_path / "clusters.json", expected
    )
    (tmp_path / selector["artifact"]).rename(tmp_path / "clusters.sqlite")
    selector["artifact"] = "clusters.sqlite"
    (tmp_path / "clusters.json").write_text(json.dumps(selector))
    assert read_json(tmp_path / "clusters.json") == expected


@pytest.mark.parametrize(
    "kind", ["lost-primary", "lost-candidate", "foreign-candidate"]
)
def test_missing_or_foreign_native_owners_cannot_retry_reference(
    source, tmp_path, kind
):
    from emuflow.phase3_candidates_native import build_native_candidate_ids
    from emuflow.partition_ir import load_partition_ir
    import shutil

    actual, _expected = catalogs(source, "positive")
    if kind == "lost-primary":
        actual["clusters"].native_artifact = None
    elif kind == "lost-candidate":
        actual["policy"][
            "eligible_combinational_cut_nets"
        ]._source.native_artifact.unlink()
    else:
        foreign = tmp_path / "foreign.emuir.sqlite"
        shutil.copyfile(actual["clusters"].native_source_path, foreign)
        eligible = build_native_candidate_ids(load_partition_ir(foreign))
        actual["policy"][
            "eligible_combinational_cut_nets"
        ] = eligible.streaming_ordered_names()
    with patch.object(
        storage,
        "_build_reference_cluster_store",
        side_effect=AssertionError("reference retry"),
    ):
        with pytest.raises(
            ValidationError,
            match="ownership is missing|generation changed|another source artifact",
        ):
            storage.write_indexed_phase3_clusters(tmp_path / "clusters.json", actual)
    assert not (tmp_path / "clusters.json").exists()


def test_resealed_candidate_names_cannot_disagree_with_source_bitmap(source, tmp_path):
    actual, expected = catalogs(source, "positive")
    output = tmp_path / "clusters.json"
    previous = storage.write_indexed_phase3_clusters(output, expected)
    before = output.read_bytes(), (tmp_path / previous["artifact"]).read_bytes()
    candidate = actual["policy"]["eligible_combinational_cut_nets"]._source
    data = bytearray(candidate.native_artifact.read_bytes())
    nets = struct.unpack_from("<Q", data, 12)[0]
    data[28 + (nets + 7) // 8] = ord("z")
    struct.pack_into("<I", data, len(data) - 4, zlib.crc32(data[:-4]))
    candidate.native_artifact.write_bytes(data)
    candidate.native_generation = native_file_generation(candidate.native_artifact)
    with pytest.raises(
        ValidationError, match="candidate name/source identity disagrees"
    ):
        storage.write_indexed_phase3_clusters(output, actual)
    assert (
        output.read_bytes() == before[0]
        and (tmp_path / previous["artifact"]).read_bytes() == before[1]
    )


def test_packed_assignment_reads_native_ordinals_without_expanding_clusters(source):
    from tests.test_phase3_assignment_native import prepare
    from emuflow.partition import build_partition_assignment

    ir, platform, constraints, clusters, parts = prepare(source)
    assignment = build_partition_assignment(
        ir,
        platform,
        clusters,
        constraints,
        parts,
        "fixture",
        0,
        _include_semantic_contract=False,
        _stream_semantic_records=True,
        _defer_derived_records=True,
    )
    plain_clusters = {**clusters, "clusters": list(clusters["clusters"])}
    plain_assignment = {
        **assignment,
        "instance_assignment": dict(assignment["instance_assignment"]),
        "cluster_assignment": dict(assignment["cluster_assignment"]),
    }
    expected = storage.pack_phase3_assignment(plain_assignment, plain_clusters)
    with patch.object(
        type(clusters["clusters"]),
        "_record",
        side_effect=AssertionError("host cluster expansion"),
    ):
        actual = storage.pack_phase3_assignment(assignment, clusters)
    assert actual == expected


def test_failed_selector_rollback_retains_recovery_and_both_payloads(tmp_path):
    output = tmp_path / "selector.json"
    output.write_text('{"previous":true}')
    previous = output.read_bytes()
    payload = tmp_path / "new.sqlite"
    payload.write_bytes(b"new generation")
    write, replace = store_publication.write_json, store_publication.os.replace

    def reject_commit(*args, **kwargs):
        write(*args, **kwargs)
        raise OSError("commit rejected")

    def reject_restore(source, destination):
        if Path(source).suffix == ".previous":
            raise OSError("rollback rejected")
        return replace(source, destination)

    with (
        patch.object(store_publication, "write_json", reject_commit),
        patch.object(store_publication.os, "replace", reject_restore),
    ):
        with pytest.raises(OSError, match="rollback rejected"):
            store_publication.commit_store_selector(
                output, {"new": True}, (), payload, True
            )
    (recovery,) = tmp_path.glob("*.previous")
    assert (
        recovery.read_bytes() == previous and payload.read_bytes() == b"new generation"
    )


@pytest.mark.parametrize(
    "kind",
    [
        "version",
        "checksum",
        "truncated",
        "trailing",
        "generation",
        "kind",
        "fixed-count",
    ],
)
def test_bad_native_catalog_requests_leave_previous_commit(source, tmp_path, kind):
    from emuflow import phase3_cluster_catalog as catalog

    actual, expected = catalogs(source, "positive")
    output = tmp_path / "clusters.json"
    previous = storage.write_indexed_phase3_clusters(output, expected)
    payload = tmp_path / previous["artifact"]
    before = output.read_bytes(), payload.read_bytes()
    run = catalog.NativeToolProcess

    def mutate_request(executable, command, **kwargs):
        request = Path(command[5])
        data = bytearray(request.read_bytes())
        if kind == "truncated":
            data = data[:-1]
        elif kind == "checksum":
            data[-1] ^= 1
        else:
            data = data[:-4]
            if kind == "version":
                struct.pack_into("<I", data, 8, 999)
            elif kind == "generation":
                struct.pack_into(
                    "<Q", data, 12, struct.unpack_from("<Q", data, 12)[0] + 1
                )
            elif kind == "trailing":
                data += b"x"
            else:
                document_bytes = struct.unpack_from("<I", data, 124)[0]
                flags = 128 + document_bytes
                assert struct.unpack_from("<I", data, flags + 4)[0] == 0
                if kind == "kind":
                    struct.pack_into("<I", data, flags + 4, 99)
                else:
                    struct.pack_into("<Q", data, flags + 8 + 56, 2**63)
            data += struct.pack("<I", zlib.crc32(data))
        request.write_bytes(data)
        return run(executable, command, **kwargs)

    with (
        patch.object(catalog, "NativeToolProcess", mutate_request),
        patch.object(
            storage,
            "_build_reference_cluster_store",
            side_effect=AssertionError("reference retry"),
        ),
    ):
        with pytest.raises(ValidationError, match="native cluster publication failed"):
            storage.write_indexed_phase3_clusters(output, actual)
    assert output.read_bytes() == before[0] and payload.read_bytes() == before[1]
    assert list(tmp_path.glob("clusters.*.sqlite")) == [payload]


@pytest.mark.parametrize("kind", ["payload", "source"])
def test_selector_role_aliases_never_overwrite_source_or_payload(tmp_path, kind):
    from emuflow.store_publication import bind_store_source

    output = tmp_path / "source.txt"
    output.write_bytes(b"source")
    if kind == "payload":
        payload = output
        sources = ()
    else:
        payload = tmp_path / "new.sqlite"
        payload.write_bytes(b"new")
        sources = (bind_store_source(output, "sta-source-document-v1"),)
    with pytest.raises(ValidationError, match="aliases"):
        store_publication.commit_store_selector(output, {}, sources, payload, True)
    assert output.read_bytes() == b"source"
