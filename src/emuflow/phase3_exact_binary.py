"""Compact binary data plane for Phase 3 Static Exact relations.

The Phase 3 assignment SQLite database remains a small relational control
plane for instance ownership and cut sinks.  The much larger Static Exact
relations are stored here as integer-coded, per-FPGA sequential sections.
Production readers validate only the sections they consume and never build a
SQLite B-tree for tens of millions of semantic rows.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import struct
import subprocess
import tempfile
import uuid
from array import array
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .combinational_cut import semantic_contract_sha256
from .errors import ValidationError
from .phase3_exact_records import (
    STATIC_EXACT_CAPTURE_BIT_ERROR,
    STATIC_EXACT_CAPTURE_BIT_MAX_BITS,
    STATIC_EXACT_CAPTURE_BIT_MAX_BYTES,
    StaticExactCaptureSequence,
    StaticExactCutNodeSequence,
    validate_static_exact_capture_bit,
)


EXACT_BINARY_SCHEMA = "emuflow.phase3-static-exact-binary/v2"
_LEGACY_EXACT_BINARY_SCHEMA = "emuflow.phase3-static-exact-binary/v1"
EXACT_BINARY_ARTIFACT = "assignment.exact.bin"
_MAGIC = b"EMUEXACT"
_VERSION = 2
_SUPPORTED_SCHEMAS = {
    1: _LEGACY_EXACT_BINARY_SCHEMA,
    2: EXACT_BINARY_SCHEMA,
}
_PREAMBLE = struct.Struct("<8sIQQ")
_U32 = struct.Struct("<I")
_SEGMENT = struct.Struct("<IBBII")
_CAPTURE = struct.Struct("<IIB")
_MISSING = 0xFFFFFFFF


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _number(value: Any, prefix: str) -> int:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValidationError(f"Static Exact {prefix} identity is invalid")
    suffix = value[len(prefix):]
    if not suffix.isdigit():
        raise ValidationError(f"Static Exact {prefix} identity is invalid")
    number = int(suffix)
    if number < 0 or number >= _MISSING:
        raise ValidationError(f"Static Exact {prefix} identity is out of range")
    return number


def _identity(prefix: str, number: int) -> str:
    return f"{prefix}{number:06d}"


def _write_string(stream, value: Any, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context} is invalid")
    encoded = value.encode("utf-8")
    if len(encoded) >= _MISSING:
        raise ValidationError(f"{context} is too large")
    stream.write(_U32.pack(len(encoded)))
    stream.write(encoded)


def _write_raw_string(stream, encoded: bytes, context: str) -> None:
    if not isinstance(encoded, bytes) or not encoded or len(encoded) >= _MISSING:
        raise ValidationError(f"{context} is invalid")
    try:
        encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{context} is invalid") from error
    stream.write(_U32.pack(len(encoded)))
    stream.write(encoded)


class _Spool:
    def __init__(self, root: Path, label: str):
        descriptor, name = tempfile.mkstemp(
            prefix=f".assignment-exact-{label}-", suffix=".tmp", dir=root
        )
        self.path = Path(name)
        self.stream = os.fdopen(descriptor, "wb")
        self.count = 0

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.flush()
            self.stream.close()

    def unlink(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)


def _copy_section(output, spool: _Spool) -> dict[str, Any]:
    spool.close()
    offset = output.tell()
    digest = hashlib.sha256()
    length = 0
    with spool.path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            output.write(chunk)
            digest.update(chunk)
            length += len(chunk)
    return {
        "offset": offset,
        "bytes": length,
        "records": spool.count,
        "sha256": digest.hexdigest(),
    }


def _append_spool(target: _Spool, source: _Spool) -> None:
    """Append one validated typed spool without decoding its records."""

    source.close()
    with source.path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            target.stream.write(chunk)
    target.count += source.count


def _validate_direct_capture_segment_ids(
    segment_ids: Sequence[bytes],
    first_number: int,
    count: int,
) -> None:
    """Check the canonical lexical view of one consecutive numeric range."""

    if (
        isinstance(first_number, bool)
        or not isinstance(first_number, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or first_number < 0
        or count < 0
        or first_number + count > _MISSING
        or len(segment_ids) != count
    ):
        raise ValidationError("Static Exact direct capture segments disagree")
    previous: bytes | None = None
    end = first_number + count
    for identity in segment_ids:
        if (
            not isinstance(identity, bytes)
            or (previous is not None and identity <= previous)
        ):
            raise ValidationError("Static Exact direct capture segments disagree")
        try:
            decoded = identity.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "Static Exact direct capture segments disagree"
            ) from error
        number = _number(decoded, "segment")
        if (
            identity != f"segment{number:06d}".encode("ascii")
            or number < first_number
            or number >= end
        ):
            raise ValidationError("Static Exact direct capture segments disagree")
        previous = identity


def _write_native_phase3_exact_binary(path, exact_contract, part_ids, source, native_paths, semantic_digest, format_version):
    """C++ fills the final data plane once; Python seals its bounded manifest."""

    from .native_tools import resolve_native_stage_command

    exact_digest = semantic_digest or semantic_contract_sha256(exact_contract)
    if not isinstance(exact_digest, str) or len(exact_digest) != 64 or any(
        character not in "0123456789abcdef" for character in exact_digest
    ):
        raise ValidationError("Static Exact semantic digest is invalid")
    schema = _SUPPORTED_SCHEMAS[format_version]
    header = {key: value for key, value in exact_contract.items() if key not in {
        "cut_nodes", "dependency_edges", "logic_segments", "capture_requirements"
    }}
    names = ["nets", "node_sources"]
    for index in range(len(part_ids)):
        names.extend((f"segments:{index}", f"captures:{index}"))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix=".native-exact-sections-", dir=path.parent) as directory:
            request, summary = Path(directory) / "request.bin", Path(directory) / "counts.bin"
            with request.open("wb") as stream:
                stream.write(struct.pack("<QIII", 0x31574553464D5545, 1, format_version, len(part_ids)))
                for value in [*part_ids, str(temporary.resolve())]:
                    raw = value.encode("utf-8")
                    if not raw or len(raw) >= 2**32:
                        raise ValidationError("native exact section identity is invalid")
                    stream.write(_U32.pack(len(raw)))
                    stream.write(raw)
            command = resolve_native_stage_command(
                "emuflow_static_exact_analyzer", "phase3", "write-exact-sections"
            )
            if len(command) == 1:
                command = [*command, "--write-exact-sections"]
            completed = subprocess.run(
                [*command, str(native_paths[0]), str(native_paths[1]), str(request), str(summary)],
                capture_output=True, text=True, check=False,
            )
            if completed.returncode:
                raise ValidationError("native exact section publication failed: " + completed.stderr.strip())
            data = summary.read_bytes()
            if len(data) != 16 + 56 * len(names) + 40:
                raise ValidationError("native exact section summary is invalid")
            magic, version, sections_count = struct.unpack_from("<QII", data)
            if magic != 0x314F5753464D5545 or version != 2 or sections_count != len(names):
                raise ValidationError("native exact section summary is invalid")
            sections = {}
            cursor = _PREAMBLE.size
            for index, name in enumerate(names):
                offset, size, count, digest = struct.unpack_from("<QQQ32s", data, 16 + 56 * index)
                if offset != cursor:
                    raise ValidationError("native exact section extents disagree")
                sections[name] = {"offset": offset, "bytes": size, "records": count, "sha256": digest.hex()}
                cursor += size
            counts = dict(zip(("cut_nodes", "source_segments", "logic_segments", "consumer_segments", "capture_requirements"),
                              struct.unpack_from("<5Q", data, 16 + 56 * len(names))))
            if counts != {
                "cut_nodes": len(source.cut_nodes), "source_segments": source.launch_count + source.dependency_count,
                "logic_segments": len(exact_contract["logic_segments"]),
                "consumer_segments": source.dependency_count + len(source.captures),
                "capture_requirements": len(source.captures),
            } or len(exact_contract["dependency_edges"]) != source.dependency_count or temporary.stat().st_size != cursor:
                raise ValidationError("native exact section population disagrees")
            manifest = {
                "schema": schema,
                **({"capture_bit_max_bits": STATIC_EXACT_CAPTURE_BIT_MAX_BITS} if format_version == 2 else {}),
                "semantic_contract_schema": exact_contract.get("schema"),
                "semantic_contract_sha256": exact_digest, "contract_header": header,
                "parts": list(part_ids), "counts": counts, "sections": sections,
            }
            manifest_bytes = _json(manifest)
            with temporary.open("r+b") as output:
                output.seek(cursor)
                output.write(manifest_bytes)
                output.seek(0)
                output.write(_PREAMBLE.pack(_MAGIC, format_version, cursor, len(manifest_bytes)))
                output.flush()
                os.fsync(output.fileno())
            descriptor = {
                "artifact": EXACT_BINARY_ARTIFACT, "schema": schema,
                **({"capture_bit_max_bits": STATIC_EXACT_CAPTURE_BIT_MAX_BITS} if format_version == 2 else {}),
                "bytes": temporary.stat().st_size, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "semantic_contract_schema": exact_contract.get("schema"), "semantic_contract_sha256": exact_digest,
                "counts": counts,
            }
        os.replace(temporary, path)
        return descriptor
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_direct_static_exact_sections(
    cut_nodes: StaticExactCutNodeSequence,
    captures: StaticExactCaptureSequence,
    part_ids: Sequence[str],
    spools: Mapping[str, _Spool],
    *,
    format_version: int,
) -> dict[str, int] | None:
    """Write native integer topology without expanding logical record dicts."""

    source = cut_nodes.direct_binary_source
    if source is None:
        return None
    if source.cut_nodes is not cut_nodes or source.captures is not captures:
        raise ValidationError("Static Exact direct binary source is invalid")
    topology = source.integer_topology
    if len(topology) != len(cut_nodes):
        raise ValidationError("Static Exact direct binary source is invalid")
    part_index = {
        fpga.encode("utf-8"): index for index, fpga in enumerate(part_ids)
    }
    if len(part_index) != len(part_ids):
        raise ValidationError("Static Exact binary parts are invalid")

    launch_count = sum(flag != 0 for flag in source.launch_flags)
    dependency_count = 0
    for cut_index in range(len(cut_nodes)):
        count = topology.dependency_count(cut_index)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError("Static Exact direct topology is invalid")
        dependency_count += count
        if dependency_count >= _MISSING:
            raise ValidationError("Static Exact segment population is too large")
    capture_count = len(captures)
    logic_segment_count = launch_count + dependency_count + capture_count
    if logic_segment_count >= _MISSING:
        raise ValidationError("Static Exact segment population is too large")

    nets_spool = spools["nets"]
    nodes_spool = spools["node_sources"]
    dependency_spools = {
        index: _Spool(
            nets_spool.path.parent, f"direct-dependencies-{index}"
        )
        for index in range(len(part_ids))
    }
    launch_number = 0
    dependency_number = launch_count
    previous_net: bytes | None = None
    try:
        for cut_index in range(len(cut_nodes)):
            (
                net,
                _cut_class,
                source_fpga,
                _sink_fpgas,
                _dependency_level,
                _combinational_depth,
                predecessors,
                source_segments,
                _capture_segments,
            ) = cut_nodes.binary_node(cut_index)
            if (
                not isinstance(net, bytes)
                or (previous_net is not None and net <= previous_net)
                or source_fpga not in part_index
            ):
                raise ValidationError("Static Exact direct cut topology is invalid")
            previous_net = net
            _write_raw_string(nets_spool.stream, net, "cut net")
            nets_spool.count += 1

            direct_dependency_count = topology.dependency_count(cut_index)
            if len(predecessors) != direct_dependency_count:
                raise ValidationError(
                    "Static Exact direct topology disagrees with cut records"
                )
            dependency_spool = dependency_spools[part_index[source_fpga]]
            previous_dependency = -1
            observed_dependencies = 0
            for ordinal, dependency_index in enumerate(
                topology.dependency_indices(cut_index)
            ):
                if (
                    ordinal >= direct_dependency_count
                    or isinstance(dependency_index, bool)
                    or not isinstance(dependency_index, int)
                    or dependency_index < 0
                    or dependency_index >= len(cut_nodes)
                    or dependency_index <= previous_dependency
                ):
                    raise ValidationError("Static Exact direct topology is invalid")
                dependency_net = cut_nodes.binary_node(dependency_index)[0]
                if predecessors[ordinal] != dependency_net:
                    raise ValidationError(
                        "Static Exact direct topology disagrees with cut records"
                    )
                dependency_spool.stream.write(
                    _SEGMENT.pack(
                        dependency_number + ordinal,
                        1,
                        0,
                        dependency_index,
                        cut_index,
                    )
                )
                dependency_spool.count += 1
                previous_dependency = dependency_index
                observed_dependencies += 1
            if observed_dependencies != direct_dependency_count:
                raise ValidationError(
                    "Static Exact direct topology disagrees with cut records"
                )

            flag = source.launch_flags[cut_index]
            source_count = int(flag != 0) + direct_dependency_count
            if len(source_segments) != source_count:
                raise ValidationError(
                    "Static Exact direct source segments disagree with cut records"
                )
            for ordinal, segment_id in enumerate(source_segments):
                number = (
                    launch_number
                    if flag and ordinal == 0
                    else dependency_number + ordinal - int(flag != 0)
                )
                if segment_id != f"segment{number:06d}".encode("ascii"):
                    raise ValidationError(
                        "Static Exact direct source segments disagree with cut records"
                    )
            nodes_spool.stream.write(_U32.pack(source_count))
            if flag:
                nodes_spool.stream.write(_U32.pack(launch_number))
            for number in range(
                dependency_number,
                dependency_number + direct_dependency_count,
            ):
                nodes_spool.stream.write(_U32.pack(number))
            nodes_spool.count += 1

            segment_spool = spools[f"segments:{part_index[source_fpga]}"]
            if flag:
                segment_spool.stream.write(
                    _SEGMENT.pack(
                        launch_number,
                        0,
                        int(flag == 2),
                        _MISSING,
                        cut_index,
                    )
                )
                segment_spool.count += 1
                launch_number += 1
            dependency_number += direct_dependency_count
        if launch_number != launch_count or dependency_number != (
            launch_count + dependency_count
        ):
            raise ValidationError("Static Exact direct segment numbering disagrees")

        # Canonical section order is all launches, then all dependencies.  The
        # temporary typed spools contain only edges checked in the single
        # analyzer-cursor pass above, so no stateful cursor is trusted twice.
        for index in range(len(part_ids)):
            _append_spool(spools[f"segments:{index}"], dependency_spools[index])
    finally:
        for spool in dependency_spools.values():
            spool.unlink()

    capture_iterator = iter(captures.iter_binary_captures())
    next_capture = next(capture_iterator, None)
    capture_number = 0
    capture_segment_start = launch_count + dependency_count
    for cut_index in range(len(cut_nodes)):
        node = cut_nodes.binary_node(cut_index)
        net = node[0]
        capture_segments = node[8]
        first_capture_number = capture_number
        while next_capture is not None and next_capture[0] == net:
            cut_net, fpga, kind, endpoint, port, bit = next_capture
            if fpga not in part_index or kind not in {0, 1}:
                raise ValidationError("Static Exact direct capture is invalid")
            expected_segment = capture_segment_start + capture_number
            capture_spool = spools[f"captures:{part_index[fpga]}"]
            capture_spool.stream.write(
                _CAPTURE.pack(capture_number, cut_index, kind)
            )
            _write_raw_string(
                capture_spool.stream,
                endpoint,
                "Static Exact capture endpoint",
            )
            if kind == 0:
                if port or bit:
                    raise ValidationError(
                        "Static Exact top-output capture is invalid"
                    )
            else:
                _write_raw_string(
                    capture_spool.stream,
                    port,
                    "Static Exact capture port",
                )
                if (
                    not bit
                    or len(bit) > STATIC_EXACT_CAPTURE_BIT_MAX_BYTES
                    or (len(bit) > 1 and bit[0] == 0)
                ):
                    raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
                if format_version == 1:
                    value = int.from_bytes(bit, "big")
                    if value >= _MISSING:
                        raise ValidationError(
                            "Static Exact capture bit is invalid"
                        )
                    capture_spool.stream.write(_U32.pack(value))
                else:
                    capture_spool.stream.write(_U32.pack(len(bit)))
                    capture_spool.stream.write(bit)
            capture_spool.count += 1

            segment_spool = spools[f"segments:{part_index[fpga]}"]
            segment_spool.stream.write(
                _SEGMENT.pack(
                    expected_segment,
                    2,
                    0,
                    cut_index,
                    capture_number,
                )
            )
            segment_spool.count += 1
            capture_number += 1
            next_capture = next(capture_iterator, None)
        if next_capture is not None and next_capture[0] < net:
            raise ValidationError("Static Exact direct capture order is invalid")
        _validate_direct_capture_segment_ids(
            capture_segments,
            capture_segment_start + first_capture_number,
            capture_number - first_capture_number,
        )
    if next_capture is not None or capture_number != capture_count:
        raise ValidationError("Static Exact direct capture coverage disagrees")

    return {
        "cut_nodes": len(cut_nodes),
        "source_segments": launch_count + dependency_count,
        "logic_segments": logic_segment_count,
        "consumer_segments": dependency_count + capture_count,
        "capture_requirements": capture_count,
    }


def write_phase3_exact_binary(
    path: Path,
    exact_contract: Mapping[str, Any],
    part_ids: Sequence[str],
    *,
    semantic_digest: str | None = None,
    _format_version: int = _VERSION,
) -> dict[str, Any]:
    """Atomically publish the large Static Exact relations without SQLite."""

    schema = _SUPPORTED_SCHEMAS.get(_format_version)
    if schema is None:
        raise ValidationError("Static Exact binary format version is invalid")
    if (
        not part_ids
        or list(part_ids) != sorted(part_ids)
        or len(set(part_ids)) != len(part_ids)
    ):
        raise ValidationError("Static Exact binary parts are invalid")
    part_index = {fpga: index for index, fpga in enumerate(part_ids)}
    cut_nodes = exact_contract.get("cut_nodes")
    segments = exact_contract.get("logic_segments")
    captures = exact_contract.get("capture_requirements")
    if (
        not isinstance(cut_nodes, list)
        or not isinstance(segments, list)
        or not isinstance(captures, list)
    ):
        raise ValidationError("Static Exact binary input is incomplete")
    if len(cut_nodes) >= _MISSING:
        raise ValidationError("Static Exact cut-node order is invalid")

    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(cut_nodes, StaticExactCutNodeSequence) and cut_nodes.direct_binary_source is not None:
        source = cut_nodes.direct_binary_source
        native_paths = source.native_record_paths()
        if native_paths is not None:
            if source.cut_nodes is not cut_nodes or source.captures is not captures or source.logic_segments is not segments or (
                source.dependency_edges is not exact_contract.get("dependency_edges")
            ):
                raise ValidationError("Static Exact direct binary generation is mixed")
            return _write_native_phase3_exact_binary(
                path, exact_contract, part_ids, source, native_paths, semantic_digest, _format_version,
            )
    spools: dict[str, _Spool] = {
        "nets": _Spool(path.parent, "nets"),
        "node_sources": _Spool(path.parent, "node-sources"),
    }
    for index in range(len(part_ids)):
        spools[f"segments:{index}"] = _Spool(path.parent, f"segments-{index}")
        spools[f"captures:{index}"] = _Spool(path.parent, f"captures-{index}")

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        direct_counts = (
            _write_direct_static_exact_sections(
                cut_nodes,
                captures,
                part_ids,
                spools,
                format_version=_format_version,
            )
            if isinstance(cut_nodes, StaticExactCutNodeSequence)
            and isinstance(captures, StaticExactCaptureSequence)
            else None
        )
        if direct_counts is not None:
            if (
                len(segments) != direct_counts["logic_segments"]
                or len(exact_contract.get("dependency_edges", []))
                != direct_counts["consumer_segments"]
                - direct_counts["capture_requirements"]
            ):
                raise ValidationError(
                    "Static Exact direct binary population disagrees"
                )
        else:
            net_ids = [
                node.get("net")
                for node in cut_nodes
                if isinstance(node, Mapping)
            ]
            if (
                len(net_ids) != len(cut_nodes)
                or any(not isinstance(net, str) or not net for net in net_ids)
                or net_ids != sorted(net_ids)
                or len(set(net_ids)) != len(net_ids)
            ):
                raise ValidationError("Static Exact cut-node order is invalid")
            net_index = {net: index for index, net in enumerate(net_ids)}
            nets_spool = spools["nets"]
            nodes_spool = spools["node_sources"]
            for node_index, node in enumerate(cut_nodes):
                _write_string(nets_spool.stream, net_ids[node_index], "cut net")
                nets_spool.count += 1
                source_ids = node.get("source_segment_ids")
                if not isinstance(source_ids, list) or len(source_ids) >= _MISSING:
                    raise ValidationError("Static Exact source segments are invalid")
                nodes_spool.stream.write(_U32.pack(len(source_ids)))
                for segment_id in source_ids:
                    nodes_spool.stream.write(
                        _U32.pack(_number(segment_id, "segment"))
                    )
                nodes_spool.count += 1

            for expected_number, segment in enumerate(segments):
                if not isinstance(segment, Mapping):
                    raise ValidationError("Static Exact segment is invalid")
                number = _number(segment.get("id"), "segment")
                fpga = segment.get("fpga")
                kind = segment.get("kind")
                if number != expected_number or fpga not in part_index:
                    raise ValidationError("Static Exact segment order is invalid")
                source = segment.get("source_cut_net")
                sink = segment.get("sink_cut_net")
                capture = segment.get("capture_requirement")
                flags = 0
                if kind == "launch_to_tx":
                    kind_code = 0
                    source_index = _MISSING
                    sink_index = net_index.get(sink, _MISSING)
                    semantics = segment.get("source_semantics")
                    if semantics is not None:
                        if semantics != "configuration-stable-constant":
                            raise ValidationError(
                                "Static Exact source semantics are invalid"
                            )
                        flags |= 1
                    if (
                        source is not None
                        or capture is not None
                        or sink_index == _MISSING
                    ):
                        raise ValidationError("Static Exact launch segment is invalid")
                elif kind == "rx_to_tx":
                    kind_code = 1
                    source_index = net_index.get(source, _MISSING)
                    sink_index = net_index.get(sink, _MISSING)
                    if capture is not None or _MISSING in {
                        source_index,
                        sink_index,
                    }:
                        raise ValidationError(
                            "Static Exact dependency segment is invalid"
                        )
                elif kind == "rx_to_capture":
                    kind_code = 2
                    source_index = net_index.get(source, _MISSING)
                    sink_index = _number(capture, "capture")
                    if sink is not None or source_index == _MISSING:
                        raise ValidationError("Static Exact capture segment is invalid")
                else:
                    raise ValidationError("Static Exact segment kind is invalid")
                spool = spools[f"segments:{part_index[fpga]}"]
                spool.stream.write(
                    _SEGMENT.pack(
                        number, kind_code, flags, source_index, sink_index
                    )
                )
                spool.count += 1

            for expected_number, capture in enumerate(captures):
                if not isinstance(capture, Mapping):
                    raise ValidationError("Static Exact capture is invalid")
                number = _number(capture.get("id"), "capture")
                fpga = capture.get("fpga")
                cut_index = net_index.get(capture.get("cut_net"), _MISSING)
                kind = capture.get("kind")
                endpoint = capture.get("endpoint")
                if (
                    number != expected_number
                    or fpga not in part_index
                    or cut_index == _MISSING
                ):
                    raise ValidationError("Static Exact capture order is invalid")
                if kind == "top-output":
                    kind_code = 0
                elif kind == "architectural-state":
                    kind_code = 1
                else:
                    kind_code = -1
                if kind_code < 0:
                    raise ValidationError("Static Exact capture kind is invalid")
                spool = spools[f"captures:{part_index[fpga]}"]
                spool.stream.write(_CAPTURE.pack(number, cut_index, kind_code))
                _write_string(
                    spool.stream, endpoint, "Static Exact capture endpoint"
                )
                if kind_code == 0:
                    if (
                        capture.get("port") is not None
                        or capture.get("bit") is not None
                    ):
                        raise ValidationError(
                            "Static Exact top-output capture is invalid"
                        )
                else:
                    _write_string(
                        spool.stream,
                        capture.get("port"),
                        "Static Exact capture port",
                    )
                    bit = validate_static_exact_capture_bit(capture.get("bit"))
                    if _format_version == 1:
                        if bit >= _MISSING:
                            raise ValidationError(
                                "Static Exact capture bit is invalid"
                            )
                        spool.stream.write(_U32.pack(bit))
                    else:
                        encoded_bit = bit.to_bytes(
                            max(1, (bit.bit_length() + 7) // 8), "big"
                        )
                        spool.stream.write(_U32.pack(len(encoded_bit)))
                        spool.stream.write(encoded_bit)
                spool.count += 1

        header = {
            key: value
            for key, value in exact_contract.items()
            if key
            not in {
                "cut_nodes",
                "dependency_edges",
                "logic_segments",
                "capture_requirements",
            }
        }
        exact_digest = semantic_digest or semantic_contract_sha256(exact_contract)
        if (
            not isinstance(exact_digest, str)
            or len(exact_digest) != 64
            or any(character not in "0123456789abcdef" for character in exact_digest)
        ):
            raise ValidationError("Static Exact semantic digest is invalid")
        with temporary.open("w+b") as output:
            output.write(_PREAMBLE.pack(_MAGIC, _format_version, 0, 0))
            sections = {
                name: _copy_section(output, spool)
                for name, spool in spools.items()
            }
            manifest = {
                "schema": schema,
                **(
                    {
                        "capture_bit_max_bits": (
                            STATIC_EXACT_CAPTURE_BIT_MAX_BITS
                        )
                    }
                    if _format_version == 2
                    else {}
                ),
                "semantic_contract_schema": exact_contract.get("schema"),
                "semantic_contract_sha256": exact_digest,
                "contract_header": header,
                "parts": list(part_ids),
                "counts": direct_counts
                or {
                    "cut_nodes": len(cut_nodes),
                    "source_segments": sum(
                        len(node["source_segment_ids"]) for node in cut_nodes
                    ),
                    "logic_segments": len(segments),
                    "consumer_segments": sum(
                        isinstance(segment.get("source_cut_net"), str)
                        for segment in segments
                    ),
                    "capture_requirements": len(captures),
                },
                "sections": sections,
            }
            manifest_bytes = _json(manifest)
            manifest_offset = output.tell()
            output.write(manifest_bytes)
            output.seek(0)
            output.write(
                _PREAMBLE.pack(
                    _MAGIC, _format_version, manifest_offset, len(manifest_bytes)
                )
            )
            output.flush()
            os.fsync(output.fileno())
        descriptor = {
            "artifact": EXACT_BINARY_ARTIFACT,
            "schema": schema,
            **(
                {
                    "capture_bit_max_bits": STATIC_EXACT_CAPTURE_BIT_MAX_BITS
                }
                if _format_version == 2
                else {}
            ),
            "bytes": temporary.stat().st_size,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "semantic_contract_schema": exact_contract.get("schema"),
            "semantic_contract_sha256": exact_digest,
            "counts": manifest["counts"],
        }
        os.replace(temporary, path)
        return descriptor
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        for spool in spools.values():
            spool.unlink()


def _load_manifest(
    path: Path, descriptor: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError("Static Exact binary artifact is missing")
    with path.open("rb") as stream:
        preamble = stream.read(_PREAMBLE.size)
        if len(preamble) != _PREAMBLE.size:
            raise ValidationError("Static Exact binary preamble is truncated")
        magic, version, offset, length = _PREAMBLE.unpack(preamble)
        size = path.stat().st_size
        expected_schema = _SUPPORTED_SCHEMAS.get(version)
        if (
            magic != _MAGIC
            or expected_schema is None
            or offset < _PREAMBLE.size
            or length <= 0
            or offset + length != size
        ):
            raise ValidationError("Static Exact binary preamble is invalid")
        stream.seek(offset)
        encoded = stream.read(length)
    try:
        manifest = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("Static Exact binary manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != expected_schema:
        raise ValidationError("Static Exact binary schema is invalid")
    if descriptor is not None:
        if (
            descriptor.get("artifact") != EXACT_BINARY_ARTIFACT
            or descriptor.get("schema") != expected_schema
            or descriptor.get("bytes") != size
            or descriptor.get("manifest_sha256")
            != hashlib.sha256(encoded).hexdigest()
            or descriptor.get("semantic_contract_schema")
            != manifest.get("semantic_contract_schema")
            or descriptor.get("semantic_contract_sha256")
            != manifest.get("semantic_contract_sha256")
            or descriptor.get("counts") != manifest.get("counts")
            or (
                version == 2
                and descriptor.get("capture_bit_max_bits")
                != STATIC_EXACT_CAPTURE_BIT_MAX_BITS
            )
        ):
            raise ValidationError("Static Exact binary descriptor disagrees")
    if (
        version == 2
        and manifest.get("capture_bit_max_bits")
        != STATIC_EXACT_CAPTURE_BIT_MAX_BITS
    ):
        raise ValidationError("Static Exact binary capture-bit limit is invalid")
    sections = manifest.get("sections")
    parts = manifest.get("parts")
    if not isinstance(sections, dict) or not isinstance(parts, list) or not parts:
        raise ValidationError("Static Exact binary manifest is incomplete")
    if (
        len(parts) > 255
        or not all(isinstance(part, str) and part for part in parts)
        or parts != sorted(parts)
        or len(parts) != len(set(parts))
    ):
        raise ValidationError("Static Exact binary parts are invalid")
    expected_sections = {"nets", "node_sources"}
    expected_sections.update(
        f"{kind}:{part}"
        for part in range(len(parts))
        for kind in ("segments", "captures")
    )
    if set(sections) != expected_sections:
        raise ValidationError("Static Exact binary sections are incomplete")
    occupied: list[tuple[int, int]] = []
    for name, section in sections.items():
        if not isinstance(name, str) or not isinstance(section, dict):
            raise ValidationError("Static Exact binary section is invalid")
        section_offset = section.get("offset")
        section_bytes = section.get("bytes")
        records = section.get("records")
        digest = section.get("sha256")
        if (
            isinstance(section_offset, bool)
            or not isinstance(section_offset, int)
            or isinstance(section_bytes, bool)
            or not isinstance(section_bytes, int)
            or isinstance(records, bool)
            or not isinstance(records, int)
            or section_offset < _PREAMBLE.size
            or section_bytes < 0
            or records < 0
            or section_offset + section_bytes > offset
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValidationError("Static Exact binary section descriptor is invalid")
        if name == "nets":
            minimum_record_bytes = _U32.size + 1
        elif name == "node_sources":
            minimum_record_bytes = _U32.size
        elif name.startswith("segments:"):
            minimum_record_bytes = _SEGMENT.size
        else:
            # Every capture contains the fixed header and one non-empty
            # endpoint string.  V2 architectural records additionally carry
            # a bounded length-prefixed unsigned bit encoding.
            minimum_record_bytes = _CAPTURE.size + _U32.size + 1
        if records > section_bytes // minimum_record_bytes:
            raise ValidationError("Static Exact binary section count is invalid")
        if section_bytes:
            occupied.append((section_offset, section_offset + section_bytes))
    occupied.sort()
    if any(
        end > occupied[index + 1][0]
        for index, (_start, end) in enumerate(occupied[:-1])
    ):
        raise ValidationError("Static Exact binary sections overlap")
    counts = manifest.get("counts")
    count_names = (
        "cut_nodes",
        "source_segments",
        "logic_segments",
        "consumer_segments",
        "capture_requirements",
    )
    if not isinstance(counts, dict) or any(
        isinstance(counts.get(name), bool)
        or not isinstance(counts.get(name), int)
        or counts[name] < 0
        for name in count_names
    ):
        raise ValidationError("Static Exact binary population counts are invalid")
    segment_records = sum(
        sections[f"segments:{part}"]["records"]
        for part in range(len(parts))
    )
    capture_records = sum(
        sections[f"captures:{part}"]["records"]
        for part in range(len(parts))
    )
    node_section = sections["node_sources"]
    maximum_source_records = (
        node_section["bytes"]
        - node_section["records"] * _U32.size
    ) // _U32.size
    if (
        counts["cut_nodes"] != sections["nets"]["records"]
        or counts["cut_nodes"] != node_section["records"]
        or counts["source_segments"] > maximum_source_records
        or counts["logic_segments"] != segment_records
        or counts["consumer_segments"] > counts["logic_segments"]
        or counts["capture_requirements"] != capture_records
    ):
        raise ValidationError("Static Exact binary population counts disagree")
    # Runtime metadata derived from the authenticated preamble.  It is never
    # serialized into the manifest or included in semantic digests.
    manifest["_format_version"] = version
    return manifest


class _SectionReader:
    def __init__(self, path: Path, section: Mapping[str, Any]):
        self.stream = path.open("rb")
        self.remaining = int(section["bytes"])
        self.expected_digest = str(section["sha256"])
        self.digest = hashlib.sha256()
        self.stream.seek(int(section["offset"]))
        self.finished = False

    def read(self, amount: int) -> bytes:
        if amount < 0 or amount > self.remaining:
            raise ValidationError("Static Exact binary record exceeds its section")
        value = self.stream.read(amount)
        if len(value) != amount:
            raise ValidationError("Static Exact binary section is truncated")
        self.remaining -= amount
        self.digest.update(value)
        return value

    def string(self) -> str:
        length = _U32.unpack(self.read(_U32.size))[0]
        try:
            value = self.read(length).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("Static Exact binary string is invalid") from error
        if not value:
            raise ValidationError("Static Exact binary string is empty")
        return value

    def finish(self) -> None:
        if self.remaining != 0:
            raise ValidationError("Static Exact binary section has trailing bytes")
        if self.digest.hexdigest() != self.expected_digest:
            raise ValidationError("Static Exact binary section digest disagrees")
        self.finished = True
        self.stream.close()

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


def _section(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = manifest["sections"].get(name)
    if not isinstance(section, Mapping):
        raise ValidationError(f"Static Exact binary section {name!r} is missing")
    return section


def _read_nets(path: Path, manifest: Mapping[str, Any]) -> list[str]:
    section = _section(manifest, "nets")
    reader = _SectionReader(path, section)
    try:
        values = [reader.string() for _ in range(int(section["records"]))]
        reader.finish()
    finally:
        reader.close()
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValidationError("Static Exact binary cut nets are invalid")
    return values


def iter_phase3_exact_node_sources(
    path: Path, descriptor: Mapping[str, Any]
) -> Iterator[tuple[str, list[int]]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    section = _section(manifest, "node_sources")
    if int(section["records"]) != len(nets):
        raise ValidationError("Static Exact binary node coverage disagrees")
    reader = _SectionReader(path, section)
    try:
        for net in nets:
            count = _U32.unpack(reader.read(_U32.size))[0]
            values = [
                _U32.unpack(reader.read(_U32.size))[0] for _ in range(count)
            ]
            yield net, values
        reader.finish()
    finally:
        reader.close()


def _validate_node_source_population(
    path: Path, manifest: Mapping[str, Any]
) -> None:
    """Prove the nested source count before count-sized validation buffers."""

    section = _section(manifest, "node_sources")
    expected = manifest["counts"]["source_segments"]
    observed = 0
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            count = _U32.unpack(reader.read(_U32.size))[0]
            if count > reader.remaining // _U32.size:
                raise ValidationError(
                    "Static Exact binary source count is invalid"
                )
            observed += count
            if observed > expected:
                raise ValidationError(
                    "Static Exact binary source population disagrees"
                )
            for _ in range(count):
                reader.read(_U32.size)
        reader.finish()
    finally:
        reader.close()
    if observed != expected:
        raise ValidationError("Static Exact binary source population disagrees")


def _part_index(manifest: Mapping[str, Any], fpga: str) -> int:
    try:
        return list(manifest["parts"]).index(fpga)
    except ValueError as error:
        raise ValidationError(f"Static Exact binary omits FPGA {fpga!r}") from error


def _iter_segments(
    path: Path,
    manifest: Mapping[str, Any],
    part: int,
    nets: Sequence[str],
) -> Iterator[dict[str, Any]]:
    fpga = manifest["parts"][part]
    section = _section(manifest, f"segments:{part}")
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            number, kind, flags, source, target = _SEGMENT.unpack(
                reader.read(_SEGMENT.size)
            )
            if kind not in {0, 1, 2} or flags & ~1:
                raise ValidationError("Static Exact binary segment flags are invalid")
            value: dict[str, Any] = {
                "id": _identity("segment", number),
                "kind": ("launch_to_tx", "rx_to_tx", "rx_to_capture")[kind],
                "fpga": fpga,
            }
            if kind == 0:
                if source != _MISSING or target >= len(nets):
                    raise ValidationError(
                        "Static Exact binary launch segment is invalid"
                    )
                value["sink_cut_net"] = nets[target]
                if flags:
                    value["source_semantics"] = "configuration-stable-constant"
            elif kind == 1:
                if flags or source >= len(nets) or target >= len(nets):
                    raise ValidationError(
                        "Static Exact binary dependency segment is invalid"
                    )
                value["source_cut_net"] = nets[source]
                value["sink_cut_net"] = nets[target]
            else:
                if flags or source >= len(nets) or target == _MISSING:
                    raise ValidationError(
                        "Static Exact binary capture segment is invalid"
                    )
                value["source_cut_net"] = nets[source]
                value["capture_requirement"] = _identity("capture", target)
            yield value
        reader.finish()
    finally:
        reader.close()


def _iter_captures(
    path: Path,
    manifest: Mapping[str, Any],
    part: int,
    nets: Sequence[str],
) -> Iterator[dict[str, Any]]:
    fpga = manifest["parts"][part]
    section = _section(manifest, f"captures:{part}")
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            number, cut_net, kind = _CAPTURE.unpack(reader.read(_CAPTURE.size))
            if cut_net >= len(nets) or kind not in {0, 1}:
                raise ValidationError("Static Exact binary capture record is invalid")
            value: dict[str, Any] = {
                "id": _identity("capture", number),
                "cut_net": nets[cut_net],
                "fpga": fpga,
                "kind": "top-output" if kind == 0 else "architectural-state",
                "endpoint": reader.string(),
            }
            if kind == 1:
                value["port"] = reader.string()
                if manifest.get("_format_version") == 1:
                    value["bit"] = _U32.unpack(reader.read(_U32.size))[0]
                else:
                    length = _U32.unpack(reader.read(_U32.size))[0]
                    if length > STATIC_EXACT_CAPTURE_BIT_MAX_BYTES:
                        raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
                    if length == 0 or length > reader.remaining:
                        raise ValidationError(
                            "Static Exact binary capture bit is invalid"
                        )
                    encoded_bit = reader.read(length)
                    if length > 1 and encoded_bit[0] == 0:
                        raise ValidationError(
                            "Static Exact binary capture bit is not canonical"
                        )
                    value["bit"] = int.from_bytes(encoded_bit, "big")
            yield value
        reader.finish()
    finally:
        reader.close()


def iter_phase3_exact_segments(
    path: Path,
    descriptor: Mapping[str, Any],
    fpga: str | None = None,
) -> Iterator[dict[str, Any]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    if fpga is not None:
        yield from _iter_segments(path, manifest, _part_index(manifest, fpga), nets)
        return
    streams = [
        _iter_segments(path, manifest, part, nets)
        for part in range(len(manifest["parts"]))
    ]
    yield from heapq.merge(
        *streams, key=lambda item: _number(item["id"], "segment")
    )


def iter_phase3_exact_captures(
    path: Path,
    descriptor: Mapping[str, Any],
    fpga: str | None = None,
) -> Iterator[dict[str, Any]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    if fpga is not None:
        yield from _iter_captures(path, manifest, _part_index(manifest, fpga), nets)
        return
    streams = [
        _iter_captures(path, manifest, part, nets)
        for part in range(len(manifest["parts"]))
    ]
    yield from heapq.merge(
        *streams, key=lambda item: _number(item["id"], "capture")
    )


def read_phase3_exact_binary_partition(
    path: Path, descriptor: Mapping[str, Any], fpga: str
) -> dict[str, Any]:
    return {
        "schema": descriptor.get("semantic_contract_schema"),
        "sha256": descriptor.get("semantic_contract_sha256"),
        "fpga": fpga,
        "logic_segments": list(iter_phase3_exact_segments(path, descriptor, fpga)),
        "capture_requirements": list(
            iter_phase3_exact_captures(path, descriptor, fpga)
        ),
    }


def read_phase3_exact_binary_contract(
    path: Path,
    descriptor: Mapping[str, Any],
    assignment: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _load_manifest(path, descriptor)
    header = manifest.get("contract_header")
    expected_digest = manifest.get("semantic_contract_sha256")
    if (
        not isinstance(header, dict)
        or header.get("schema") != manifest.get("semantic_contract_schema")
        or assignment.get("semantic_contract_sha256") != expected_digest
    ):
        raise ValidationError("Static Exact binary identity is incomplete")
    logic_segments = list(iter_phase3_exact_segments(path, descriptor))
    capture_requirements = list(iter_phase3_exact_captures(path, descriptor))
    source_segments = {
        net: [_identity("segment", number) for number in numbers]
        for net, numbers in iter_phase3_exact_node_sources(path, descriptor)
    }
    capture_segments: dict[str, list[str]] = {}
    for segment in logic_segments:
        if segment["kind"] == "rx_to_capture":
            capture_segments.setdefault(segment["source_cut_net"], []).append(
                segment["id"]
            )
    cut_by_net = {
        cut.get("net"): cut
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, Mapping) and isinstance(cut.get("net"), str)
    }
    node_fields = (
        "net",
        "cut_class",
        "source_fpgas",
        "sink_fpgas",
        "dependency_level",
        "combinational_dependency_depth",
        "predecessor_cut_nets",
    )
    cut_nodes = []
    for net in sorted(source_segments):
        cut = cut_by_net.get(net)
        if cut is None or any(field not in cut for field in node_fields):
            raise ValidationError("Static Exact binary cut node is incomplete")
        cut_nodes.append(
            {
                **{field: cut[field] for field in node_fields},
                "source_segment_ids": source_segments[net],
                "capture_segment_ids": sorted(capture_segments.get(net, [])),
            }
        )
    dependency_edges = [
        {
            "from": segment["source_cut_net"],
            "to": segment["sink_cut_net"],
            "segment": segment["id"],
        }
        for segment in logic_segments
        if segment["kind"] == "rx_to_tx"
    ]
    dependency_edges.sort(key=lambda item: (item["to"], item["from"]))
    contract = {
        **header,
        "cut_nodes": cut_nodes,
        "dependency_edges": dependency_edges,
        "logic_segments": logic_segments,
        "capture_requirements": capture_requirements,
    }
    if semantic_contract_sha256(contract) != expected_digest:
        raise ValidationError("reconstructed Static Exact binary digest disagrees")
    return contract


def phase3_exact_binary_counts(
    path: Path, descriptor: Mapping[str, Any]
) -> dict[str, int]:
    manifest = _load_manifest(path, descriptor)
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValidationError("Static Exact binary counts are invalid")
    return {key: int(value) for key, value in counts.items()}


def validate_phase3_exact_binary(
    path: Path, descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    """Independently scan every section with bounded integer state."""

    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    net_index = {net: index for index, net in enumerate(nets)}
    # Source population is nested inside one record per cut.  Scan it before
    # allocating any buffer sized by manifest population counts.
    _validate_node_source_population(path, manifest)
    counts = manifest.get("counts", {})
    segment_count = counts.get("logic_segments")
    capture_count = counts.get("capture_requirements")
    if (
        isinstance(segment_count, bool)
        or not isinstance(segment_count, int)
        or isinstance(capture_count, bool)
        or not isinstance(capture_count, int)
        or segment_count < 0
        or capture_count < 0
        or counts.get("cut_nodes") != len(nets)
    ):
        raise ValidationError("Static Exact binary population counts are invalid")

    capture_seen = bytearray(capture_count)
    capture_parts = bytearray(capture_count)
    capture_nets = array("I", [_MISSING]) * capture_count
    observed_captures = 0
    for part, fpga in enumerate(manifest["parts"]):
        if part >= 255 or not isinstance(fpga, str) or not fpga:
            raise ValidationError("Static Exact binary part is invalid")
        for capture in _iter_captures(path, manifest, part, nets):
            number = _number(capture["id"], "capture")
            if number >= capture_count or capture_seen[number]:
                raise ValidationError("Static Exact binary capture identity repeats")
            capture_seen[number] = 1
            capture_parts[number] = part
            capture_nets[number] = net_index[capture["cut_net"]]
            observed_captures += 1
    if observed_captures != capture_count or (capture_count and not all(capture_seen)):
        raise ValidationError("Static Exact binary capture coverage disagrees")

    segment_seen = bytearray(segment_count)
    segment_target_nets = array("I", [_MISSING]) * segment_count
    capture_bound = bytearray(capture_count)
    dependency_edges = 0
    consumer_segments = 0
    observed_segments = 0
    for part in range(len(manifest["parts"])):
        for segment in _iter_segments(path, manifest, part, nets):
            number = _number(segment["id"], "segment")
            if number >= segment_count or segment_seen[number]:
                raise ValidationError("Static Exact binary segment identity repeats")
            segment_seen[number] = 1
            observed_segments += 1
            if segment["kind"] == "rx_to_tx":
                dependency_edges += 1
                consumer_segments += 1
                segment_target_nets[number] = net_index[
                    segment["sink_cut_net"]
                ]
            elif segment["kind"] == "rx_to_capture":
                consumer_segments += 1
                capture = _number(segment["capture_requirement"], "capture")
                if (
                    capture >= capture_count
                    or capture_bound[capture]
                    or capture_parts[capture] != part
                    or nets[capture_nets[capture]] != segment["source_cut_net"]
                ):
                    raise ValidationError("Static Exact capture binding disagrees")
                capture_bound[capture] = 1
            else:
                segment_target_nets[number] = net_index[
                    segment["sink_cut_net"]
                ]
    if observed_segments != segment_count or (segment_count and not all(segment_seen)):
        raise ValidationError("Static Exact binary segment coverage disagrees")
    if capture_count and not all(capture_bound):
        raise ValidationError("Static Exact binary capture binding is incomplete")
    if consumer_segments != counts.get("consumer_segments"):
        raise ValidationError("Static Exact consumer population disagrees")

    source_seen = bytearray(segment_count)
    source_count = 0
    node_count = 0
    for net, source_numbers in iter_phase3_exact_node_sources(path, descriptor):
        node_count += 1
        target_net = net_index[net]
        for number in source_numbers:
            if (
                number >= segment_count
                or source_seen[number]
                or segment_target_nets[number] != target_net
            ):
                raise ValidationError("Static Exact source segment is invalid")
            source_seen[number] = 1
            source_count += 1
    if node_count != len(nets) or source_count != counts.get("source_segments"):
        raise ValidationError("Static Exact source population disagrees")
    if any(
        target != _MISSING and not source_seen[number]
        for number, target in enumerate(segment_target_nets)
    ):
        raise ValidationError("Static Exact source coverage is incomplete")

    metrics = manifest.get("contract_header", {}).get("metrics", {})
    if (
        metrics.get("transported_cut_nets") != len(nets)
        or metrics.get("logic_segments") != segment_count
        or metrics.get("capture_requirements") != capture_count
        or metrics.get("dependency_edges") != dependency_edges
    ):
        raise ValidationError("Static Exact binary metrics disagree")
    return {
        "status": "pass",
        "schema": manifest["schema"],
        **(
            {
                "capture_bit_max_bits": STATIC_EXACT_CAPTURE_BIT_MAX_BITS
            }
            if manifest["_format_version"] == 2
            else {}
        ),
        "semantic_contract_sha256": manifest["semantic_contract_sha256"],
        "cut_nodes": len(nets),
        "source_segments": source_count,
        "logic_segments": segment_count,
        "capture_requirements": capture_count,
        "dependency_edges": dependency_edges,
    }

