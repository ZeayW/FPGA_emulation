"""Typed native cut publication, independently compared with the SQL writer."""

import sqlite3
import struct
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.partition import (
    _StaticExactCutNetRecords,
    _PackedClusterAssignment,
    build_partition_assignment,
)
from emuflow.phase3_assignment_index import write_phase3_assignment_index
from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT
from emuflow.phase3_assignment_native import native_assignment_cut_source
from tests.test_phase3_assignment_native import prepare, _published_rows
from tests.test_phase3_clusters_native import source


def _assignment(source, mode="normal"):
    if mode in ("unicode", "sparse"):
        with closing(sqlite3.connect(source[0].path)) as db, db:
            if mode == "unicode":
                db.execute("UPDATE net_records SET id='é雪😀' WHERE seq=0")
            else:
                db.execute("UPDATE endpoint_records SET ordinal=ordinal*7+3")
    ir, platform, constraints, clusters, parts = prepare(source)
    if mode == "no-cuts":
        parts = _PackedClusterAssignment(("a", "b"), [0] * len(parts))
    with patch(
        "emuflow.combinational_cut.static_exact_analysis_engine",
        return_value="native-cpp-v1",
    ):
        assignment = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            parts,
            "fixture",
            7,
            _include_semantic_contract=True,
            _stream_semantic_records=True,
        )
    assert isinstance(assignment["cut_nets"], list)
    typed = native_assignment_cut_source(assignment)
    assert typed is not None
    return ir, assignment, typed


@pytest.mark.parametrize("mode", ["normal", "unicode", "sparse", "no-cuts"])
def test_native_cut_publication_matches_reference_without_host_records(
    source, tmp_path, mode
):
    import emuflow.phase3_assignment_native as producer

    ir, assignment, typed = _assignment(source, mode)
    reference = dict(
        assignment, instance_assignment=dict(assignment["instance_assignment"].items())
    )
    expected, actual = (
        tmp_path / "reference/index.sqlite",
        tmp_path / "native/index.sqlite",
    )
    reference_result = write_phase3_assignment_index(expected, ir, reference)
    publish = producer.publish_native_assignment_index
    calls = []

    def guarded(*args, **kwargs):
        calls.append(True)
        with patch.object(
            _StaticExactCutNetRecords,
            "_record",
            side_effect=AssertionError("host cut record"),
        ):
            with patch(
                "emuflow.phase3_assignment_index._normalized_cut_record",
                side_effect=AssertionError("host normalization"),
            ):
                return publish(*args, **kwargs)

    with patch.object(producer, "publish_native_assignment_index", side_effect=guarded):
        result = write_phase3_assignment_index(actual, ir, assignment)
    assert calls == [True]
    assert _published_rows(actual) == _published_rows(expected)
    assert (actual.parent / EXACT_BINARY_ARTIFACT).read_bytes() == (
        expected.parent / EXACT_BINARY_ARTIFACT
    ).read_bytes()
    assert result["cut_records_sha256"] == reference_result["cut_records_sha256"]
    assert result["native_writer_metrics"]["host_cut_records"] == 0
    assert result["native_writer_metrics"]["native_cut_records"] == len(
        assignment["cut_nets"]
    )
    if isinstance(assignment["cut_nets"], _StaticExactCutNetRecords):
        assert typed.endpoint_counts is assignment["cut_nets"]._cuts._sink_endpoints
    else:
        assert list(typed.endpoint_counts) == [
            row["sink_endpoints"] for row in assignment["cut_nets"]
        ]


@pytest.mark.parametrize(
    "kind",
    [
        "nodes-chmod",
        "captures-chmod",
        "missing-generation",
        "lost-owner",
        "lost-path",
        "mixed-owner",
        "count",
    ],
)
def test_native_cut_source_rejection_preserves_existing_pair(source, tmp_path, kind):
    ir, assignment, typed = _assignment(source)
    nodes = assignment["semantic_contract"]["cut_nodes"]
    if kind.endswith("chmod"):
        p = typed.paths[0 if kind == "nodes-chmod" else 1]
        p.chmod(p.stat().st_mode ^ 0o100)
    elif kind == "missing-generation":
        del nodes.native_semantic_generation
    elif kind == "lost-owner":
        nodes._direct_binary_source = None
    elif kind == "lost-path":
        del nodes.native_semantic_artifact_path
    elif kind == "mixed-owner":
        assignment["semantic_contract"] = dict(
            assignment["semantic_contract"], cut_nodes=[]
        )
    else:
        assignment["cut_nets"]._cuts._sink_endpoints.append(1)
    index = tmp_path / "index.sqlite"
    index.write_bytes(b"prior-index")
    binary = tmp_path / EXACT_BINARY_ARTIFACT
    binary.write_bytes(b"prior-binary")
    with pytest.raises(ValidationError):
        write_phase3_assignment_index(index, ir, assignment)
    assert (
        index.read_bytes() == b"prior-index" and binary.read_bytes() == b"prior-binary"
    )
    assert not list(tmp_path.glob(".*.tmp*"))


@pytest.mark.parametrize(
    "kind",
    [
        "checksum",
        "truncated",
        "node-generation",
        "node-count",
        "zero-endpoints",
        "changed-nodes",
    ],
)
def test_native_request_rejection_is_atomic_and_never_imports_host_cuts(
    source, tmp_path, kind
):
    import zlib
    import emuflow.phase3_assignment_native as producer
    from emuflow.tool_runtime import NativeToolProcess

    ir, assignment, typed = _assignment(source)
    assert len(assignment["cut_nets"])
    index = tmp_path / "index.sqlite"
    index.write_bytes(b"prior-index")
    binary = tmp_path / EXACT_BINARY_ARTIFACT
    binary.write_bytes(b"prior-binary")
    observed = []

    def corrupt(executable, command, **kwargs):
        request = Path(command[5])
        data = bytearray(request.read_bytes())
        assert struct.unpack_from("<I", data, 8)[0] == 2 and len(command) == 8
        observed.append(True)
        if kind == "checksum":
            data[-1] ^= 1
        elif kind == "truncated":
            data = data[:-1]
        elif kind == "node-generation":
            data[12 + 2 * 56] ^= 1
        elif kind == "node-count":
            struct.pack_into(
                "<Q",
                data,
                len(data) - 4 - 8 * len(typed.endpoint_counts) - 8,
                len(typed.endpoint_counts) + 1,
            )
        elif kind == "zero-endpoints":
            struct.pack_into(
                "<Q", data, len(data) - 4 - 8 * len(typed.endpoint_counts), 0
            )
        else:
            with typed.paths[0].open("ab") as output:
                output.write(b"changed")
        if kind not in ("checksum", "truncated"):
            struct.pack_into("<I", data, len(data) - 4, zlib.crc32(data[:-4]))
        request.write_bytes(data)
        return NativeToolProcess(executable, command, **kwargs)

    with patch.object(producer, "NativeToolProcess", side_effect=corrupt):
        with pytest.raises(ValidationError):
            write_phase3_assignment_index(index, ir, assignment)
    assert observed == [True]
    assert (
        index.read_bytes() == b"prior-index" and binary.read_bytes() == b"prior-binary"
    )
    assert not list(tmp_path.glob(".*.tmp*"))


@pytest.mark.parametrize(
    "kind",
    [
        "truncated",
        "version",
        "offset",
        "field-count",
        "source",
        "self-sink",
        "utf8",
        "launch",
    ],
)
def test_native_node_decoder_rejects_bound_malformed_records(source, tmp_path, kind):
    import zlib
    import emuflow.phase3_assignment_native as producer
    from emuflow.native_tools import native_file_generation
    from emuflow.tool_runtime import NativeToolProcess

    ir, assignment, typed = _assignment(source)
    original = typed.paths[0].read_bytes()
    data = bytearray(original)
    _, _, count, _ = struct.unpack_from("<QIIQ", data)
    assert count
    start = 24 + 8 * (count + 1)
    cursor = start

    def string():
        nonlocal cursor
        size = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        field = slice(cursor, cursor + size)
        cursor += size
        return field

    net, cut_class, owner = string(), string(), string()
    cursor += 8  # dependency level and combinational depth
    fields = cursor
    cursor += 16  # four list counts
    sink = string()
    if kind == "truncated":
        del data[-1:]
    elif kind == "version":
        struct.pack_into("<I", data, 8, 99)
    elif kind == "offset":
        struct.pack_into("<Q", data, 32, 0)
    elif kind == "field-count":
        struct.pack_into("<I", data, fields, 2**32 - 1)
    elif kind == "source":
        data[owner] = b"?" * (owner.stop - owner.start)
    elif kind == "self-sink":
        assert sink.stop - sink.start == owner.stop - owner.start
        data[sink] = data[owner]
    elif kind == "utf8":
        data[net.start] = 255
    else:
        end = struct.unpack_from("<Q", data, 32)[0]
        data[start + end - 1] = 3
    malformed = tmp_path / "malformed-nodes.bin"
    malformed.write_bytes(data)
    index, binary = tmp_path / "index.sqlite", tmp_path / EXACT_BINARY_ARTIFACT
    index.write_bytes(b"prior-index")
    binary.write_bytes(b"prior-binary")
    observed = []

    def use_malformed(executable, command, **kwargs):
        # Bind the new file generation so this tests parsing, not stale-source
        # rejection. The original producer-owned semantic files stay untouched.
        request = Path(command[5])
        data = bytearray(request.read_bytes())
        struct.pack_into("<3Q4q", data, 12 + 2 * 56, *native_file_generation(malformed))
        struct.pack_into("<I", data, len(data) - 4, zlib.crc32(data[:-4]))
        request.write_bytes(data)
        command = [*command[:6], str(malformed), *command[7:]]
        observed.append(True)
        return NativeToolProcess(executable, command, **kwargs)

    with patch.object(producer, "NativeToolProcess", side_effect=use_malformed):
        with pytest.raises(
            ValidationError, match="native assignment publication failed"
        ):
            write_phase3_assignment_index(index, ir, assignment)
    assert observed == [True]
    assert typed.paths[0].read_bytes() == original
    assert (
        index.read_bytes() == b"prior-index" and binary.read_bytes() == b"prior-binary"
    )
    assert not list(tmp_path.glob(".*.tmp*"))


@pytest.mark.parametrize("changed_source", [0, 1])
def test_original_semantic_bindings_survive_independent_index_validation(
    source, tmp_path, changed_source
):
    import emuflow.phase3_assignment_index as index_module

    ir, assignment, typed = _assignment(source)
    index = tmp_path / "index.sqlite"
    index.write_bytes(b"prior-index")
    binary = tmp_path / EXACT_BINARY_ARTIFACT
    binary.write_bytes(b"prior-binary")
    validate = index_module.validate_phase3_assignment_index

    def changed(*args, **kwargs):
        result = validate(*args, **kwargs)
        p = typed.paths[changed_source]
        p.chmod(p.stat().st_mode ^ 0o100)
        return result

    with patch.object(
        index_module, "validate_phase3_assignment_index", side_effect=changed
    ):
        with pytest.raises(ValidationError):
            write_phase3_assignment_index(index, ir, assignment)
    assert (
        index.read_bytes() == b"prior-index" and binary.read_bytes() == b"prior-binary"
    )
