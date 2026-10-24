"""Repeatable typed record streams for the Static Exact data plane.

The logical v3 semantic contract remains a mapping containing JSON arrays.
Production Phase 3, however, does not need to retain a Python dictionary for
every cut node, dependency edge, and logic segment before publishing
``assignment.exact.bin``.  These read-only list subclasses retain canonical
cut-node/capture relations in compact byte buffers and generate derived records
on each traversal, so the canonical JSON hasher and exact-binary writer can
consume the same ordered record stream directly.
"""

from __future__ import annotations

import hashlib
import json
import mmap
from pathlib import Path
import struct
import subprocess
import tempfile
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from json.encoder import encode_basestring_ascii
from typing import Any

from .errors import ValidationError


STATIC_EXACT_RECORD_STREAM_SCHEMA = "emuflow.static-exact-record-stream/v1"
STATIC_EXACT_CAPTURE_BIT_MAX_BITS = 8192
STATIC_EXACT_CAPTURE_BIT_MAX_BYTES = STATIC_EXACT_CAPTURE_BIT_MAX_BITS // 8
STATIC_EXACT_CAPTURE_BIT_ERROR = (
    "Static Exact capture bit must be a non-negative integer of at most "
    f"{STATIC_EXACT_CAPTURE_BIT_MAX_BITS} bits"
)
# Kind plus byte lengths for cut, FPGA, endpoint, port, and a canonical
# non-negative big-endian bit. Bits do not use a fixed native integer, but the
# shared 8192-bit limit keeps JSON hashing and binary decoding bounded across
# supported Python versions.
_CAPTURE_HEADER = struct.Struct("<BIIIII")
_CUT_COUNTS = struct.Struct("<6I")
_STRING_LENGTH = struct.Struct("<I")
_CUT_NODE_FIELDS = {
    "net",
    "cut_class",
    "source_fpgas",
    "sink_fpgas",
    "dependency_level",
    "combinational_dependency_depth",
    "predecessor_cut_nets",
    "source_segment_ids",
    "capture_segment_ids",
}


def validate_static_exact_capture_bit(value: Any) -> int:
    """Validate a capture bit before integer formatting or byte encoding."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value.bit_length() > STATIC_EXACT_CAPTURE_BIT_MAX_BITS
    ):
        raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
    return value


class StaticExactRecordSequence(list):
    """A read-only, repeatable logical JSON array backed by a record factory."""

    def __init__(
        self,
        kind: str,
        count: int,
        factory: Callable[[], Iterator[dict[str, Any]]],
    ) -> None:
        super().__init__()
        if not kind or count < 0:
            raise ValueError("Static Exact record stream descriptor is invalid")
        self.kind = kind
        self.record_stream_schema = STATIC_EXACT_RECORD_STREAM_SCHEMA
        self._count = count
        self._factory = factory

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self._factory()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Static Exact record index must be an integer")
        resolved = index if index >= 0 else self._count + index
        if resolved < 0 or resolved >= self._count:
            raise IndexError("Static Exact record index out of range")
        for ordinal, record in enumerate(self):
            if ordinal == resolved:
                return record
        raise ValidationError("Static Exact record stream ended early")

    def close(self) -> None:
        """Release an explicitly owned native mapping before retiring its file."""
        mapping = getattr(self, "_native_mapping", None)
        if mapping is not None:
            data, views = mapping
            for view in views:
                view.release()
            data.close()
            self._native_mapping = None

    def _immutable(self, *args, **kwargs):
        raise TypeError("Static Exact record streams are immutable")

    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __delitem__ = _immutable
    __setitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


class StaticExactCutNodeSequence(StaticExactRecordSequence):
    """A repeatable cut-node stream with direct indexed record decoding."""

    def __init__(
        self,
        count: int,
        factory: Callable[[], Iterator[dict[str, Any]]],
        indexer: Callable[[int], dict[str, Any]],
    ) -> None:
        super().__init__("cut_nodes", count, factory)
        self._indexer = indexer
        self._binary_decoder: Callable[[int], tuple[Any, ...]] | None = None
        self._direct_binary_source: StaticExactDirectBinarySource | None = None

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._indexer(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Static Exact record index must be an integer")
        resolved = index if index >= 0 else len(self) + index
        if resolved < 0 or resolved >= len(self):
            raise IndexError("Static Exact record index out of range")
        return self._indexer(resolved)

    def _bind_binary_decoder(
        self, decoder: Callable[[int], tuple[Any, ...]]
    ) -> None:
        self._binary_decoder = decoder

    def binary_node(self, index: int) -> tuple[Any, ...]:
        """Return one validated node as raw UTF-8 fields for typed writers."""

        if self._binary_decoder is None:
            raise ValidationError("Static Exact binary node source is unavailable")
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Static Exact record index must be an integer")
        if index < 0 or index >= len(self):
            raise IndexError("Static Exact record index out of range")
        return self._binary_decoder(index)

    @property
    def direct_binary_source(self) -> "StaticExactDirectBinarySource | None":
        return self._direct_binary_source

    def bind_direct_binary_source(
        self,
        captures: "StaticExactCaptureSequence",
        launch_flags: bytes | bytearray,
        integer_topology: Any,
        dependency_edges: "StaticExactRecordSequence",
        logic_segments: "StaticExactRecordSequence",
        dependency_count: int,
    ) -> None:
        """Attach the native integer relation used by the exact-binary writer.

        The logical record streams remain the canonical digest surface.  This
        private production attachment lets the typed binary publisher consume
        the already validated CSR relation without expanding dependency and
        logic-segment dictionaries a second time.
        """

        self._direct_binary_source = StaticExactDirectBinarySource(
            self,
            captures,
            launch_flags,
            integer_topology,
            dependency_edges,
            logic_segments,
            dependency_count,
        )


class StaticExactCaptureSequence(StaticExactRecordSequence):
    """A capture stream with a raw typed cursor for binary publication."""

    def __init__(
        self,
        count: int,
        factory: Callable[[], Iterator[dict[str, Any]]],
        binary_factory: Callable[
            [], Iterator[tuple[bytes, bytes, int, bytes, bytes, bytes]]
        ],
    ) -> None:
        super().__init__("capture_requirements", count, factory)
        self._binary_factory = binary_factory

    def binary_capture(self, index: int):
        if index < 0 or index >= len(self):
            raise IndexError(index)
        if hasattr(self, "_binary_indexer"):
            return self._binary_indexer(index)
        return next(row for seq, row in enumerate(self.iter_binary_captures()) if seq == index)

    def iter_binary_captures(
        self,
    ) -> Iterator[tuple[bytes, bytes, int, bytes, bytes, bytes]]:
        return self._binary_factory()


class StaticExactDirectBinarySource:
    """Compact native topology consumed directly by assignment.exact.bin.

    ``integer_topology`` is the independently finalized analyzer relation.  It
    deliberately exposes only bounded integer counts/cursors; the publisher
    checks every integer predecessor against the canonical cut-node strings
    before emitting bytes, so a stale or mixed relation fails closed.
    """

    def __init__(
        self,
        cut_nodes: StaticExactCutNodeSequence,
        captures: StaticExactCaptureSequence,
        launch_flags: bytes | bytearray,
        integer_topology: Any,
        dependency_edges: StaticExactRecordSequence,
        logic_segments: StaticExactRecordSequence,
        dependency_count: int,
    ) -> None:
        if (
            len(launch_flags) != len(cut_nodes)
            or any(flag not in {0, 1, 2} for flag in launch_flags)
            or not hasattr(integer_topology, "dependency_count")
            or not hasattr(integer_topology, "dependency_indices")
            or isinstance(dependency_count, bool)
            or not isinstance(dependency_count, int)
            or dependency_count < 0
        ):
            raise ValidationError("Static Exact direct binary source is invalid")
        try:
            topology_count = len(integer_topology)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "Static Exact direct binary source is invalid"
            ) from error
        if topology_count != len(cut_nodes):
            raise ValidationError("Static Exact direct binary source is invalid")
        self.cut_nodes = cut_nodes
        self.captures = captures
        self.launch_flags = bytes(launch_flags)
        self.integer_topology = integer_topology
        self.dependency_edges = dependency_edges
        self.logic_segments = logic_segments
        self.launch_count = sum(flag != 0 for flag in self.launch_flags)
        self.dependency_count = dependency_count
        paths = (getattr(cut_nodes, "native_semantic_artifact_path", None),
                 getattr(captures, "native_capture_artifact_path", None))
        self._native_record_refs = tuple(_native_record_identity(path) for path in paths) if all(path is not None for path in paths) else None

    def native_record_paths(self):
        paths = (getattr(self.cut_nodes, "native_semantic_artifact_path", None),
                 getattr(self.captures, "native_capture_artifact_path", None))
        if self._native_record_refs is None:
            if all(path is not None for path in paths):
                raise ValidationError("native semantic generation is mixed")
            return None
        if any(path is None for path in paths) or tuple(_native_record_identity(path) for path in paths) != self._native_record_refs:
            raise ValidationError("native semantic generation is mixed")
        return tuple(reference[0] for reference in self._native_record_refs)


def _native_record_identity(path):
    try:
        path = Path(path).resolve()
        metadata = path.stat()
        return path, metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns
    except OSError as error:
        raise ValidationError("native semantic generation is unavailable") from error


def build_static_exact_cut_node_stream(
    nodes: Iterable[Mapping[str, Any]],
) -> StaticExactCutNodeSequence:
    """Pack canonical v3 cut nodes with a compact random-access offset table."""

    encoded = bytearray()
    offsets = array("Q")
    previous_net: str | None = None

    def append_string(value: Any) -> None:
        if not isinstance(value, str) or not value:
            raise ValidationError("Static Exact cut-node stream is invalid")
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValidationError(
                "Static Exact cut-node identity is invalid"
            ) from error
        if len(raw) >= 2**32:
            raise ValidationError("Static Exact cut-node identity is too large")
        encoded.extend(_STRING_LENGTH.pack(len(raw)))
        encoded.extend(raw)

    def string_list(node: Mapping[str, Any], name: str) -> list[str]:
        value = node.get(name)
        if (
            not isinstance(value, list)
            or len(value) >= 2**32
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise ValidationError("Static Exact cut-node stream is invalid")
        return value

    for node in nodes:
        if not isinstance(node, Mapping) or set(node) != _CUT_NODE_FIELDS:
            raise ValidationError("Static Exact cut-node stream is invalid")
        net = node.get("net")
        cut_class = node.get("cut_class")
        source_fpgas = string_list(node, "source_fpgas")
        sink_fpgas = string_list(node, "sink_fpgas")
        predecessors = string_list(node, "predecessor_cut_nets")
        source_segments = string_list(node, "source_segment_ids")
        capture_segments = string_list(node, "capture_segment_ids")
        dependency_level = node.get("dependency_level")
        combinational_depth = node.get("combinational_dependency_depth")
        if (
            not isinstance(net, str)
            or not net
            or not isinstance(cut_class, str)
            or not cut_class
            or len(source_fpgas) != 1
            or isinstance(dependency_level, bool)
            or not isinstance(dependency_level, int)
            or dependency_level < 0
            or dependency_level >= 2**32
            or isinstance(combinational_depth, bool)
            or not isinstance(combinational_depth, int)
            or combinational_depth < 0
            or combinational_depth >= 2**32
            or (previous_net is not None and net <= previous_net)
        ):
            raise ValidationError("Static Exact cut-node stream is invalid")
        previous_net = net
        offsets.append(len(encoded))
        append_string(net)
        append_string(cut_class)
        append_string(source_fpgas[0])
        encoded.extend(
            _CUT_COUNTS.pack(
                dependency_level,
                combinational_depth,
                len(sink_fpgas),
                len(predecessors),
                len(source_segments),
                len(capture_segments),
            )
        )
        for values in (
            sink_fpgas,
            predecessors,
            source_segments,
            capture_segments,
        ):
            for value in values:
                append_string(value)

    return _decode_cut_node_stream(encoded, offsets)


def read_native_static_exact_cut_node_stream(path: Path) -> StaticExactCutNodeSequence:
    """Map final native-owned node bytes without repacking their records."""

    data = None
    views = []
    result = None
    try:
        with path.open("rb") as stream:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        if len(data) < 32:
            raise ValidationError("native semantic node header is truncated")
        magic, version, count, size = struct.unpack_from("<QIIQ", data)
        start = 24 + 8 * (count + 1)
        if magic != 0x314E4553464D5545 or version not in (1, 2) or start + size != len(data):
            raise ValidationError("native semantic node envelope is invalid")
        offsets = memoryview(data)[24:start].cast("Q")
        views.append(offsets)
        if offsets[0] != 0 or offsets[-1] != size or any(
            offsets[index] >= offsets[index + 1] for index in range(count)
        ):
            raise ValidationError("native semantic node offsets are invalid")
        encoded, node_offsets = memoryview(data)[start:], offsets[:-1]
        views.extend((encoded, node_offsets))
        result = _decode_cut_node_stream(encoded, node_offsets, with_flags=version == 2)
        result.native_semantic_version = version
        if version == 2:
            result.native_launch_flag = lambda index: data[start + offsets[index + 1] - 1]
            raw_decoder = result._binary_decoder
            def dependency_indices(index):
                predecessor_count = len(raw_decoder(index)[6])
                end = start + offsets[index + 1] - 1
                return struct.unpack_from(f"<{predecessor_count}I", data, end - 4 * predecessor_count)
            result.native_dependency_indices = dependency_indices
        result.native_semantic_artifact_path = path
        result._native_mapping = (data, views)
        return result
    except (OSError, ValueError, struct.error) as error:
        raise ValidationError("native semantic node artifact is invalid") from error

    finally:
        if result is None or getattr(result, "_native_mapping", None) is None:
            for view in views:
                view.release()
            if data is not None:
                data.close()


def _decode_cut_node_stream(encoded, offsets, *, with_flags=False) -> StaticExactCutNodeSequence:
    count = len(offsets)

    def decode_fields(index: int, *, raw: bool) -> tuple[Any, ...]:
        view = encoded
        offset = offsets[index]
        end = offsets[index + 1] if index + 1 < count else len(view)

        def read_string() -> str | bytes:
            nonlocal offset
            if offset + _STRING_LENGTH.size > end:
                raise ValidationError("Static Exact cut-node stream ended early")
            (length,) = _STRING_LENGTH.unpack_from(view, offset)
            offset += _STRING_LENGTH.size
            value_end = offset + length
            if value_end > end:
                raise ValidationError("Static Exact cut-node stream ended early")
            encoded_value = bytes(view[offset:value_end])
            offset = value_end
            if not encoded_value:
                raise ValidationError("Static Exact cut-node stream is invalid")
            if raw:
                return encoded_value
            try:
                return encoded_value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError(
                    "Static Exact cut-node identity is invalid"
                ) from error

        net = read_string()
        cut_class = read_string()
        source_fpga = read_string()
        if offset + _CUT_COUNTS.size > end:
            raise ValidationError("Static Exact cut-node stream ended early")
        (
            dependency_level,
            combinational_depth,
            sink_count,
            predecessor_count,
            source_count,
            capture_count,
        ) = _CUT_COUNTS.unpack_from(view, offset)
        offset += _CUT_COUNTS.size

        def read_strings(size: int) -> list[str]:
            return [read_string() for _ in range(size)]

        sink_fpgas = read_strings(sink_count)
        predecessors = read_strings(predecessor_count)
        source_segments = read_strings(source_count)
        capture_segments = read_strings(capture_count)
        if with_flags:
            if offset + 4 * predecessor_count + 1 != end:
                raise ValidationError("native semantic coordinates are truncated")
            previous = -1
            for _ in range(predecessor_count):
                ordinal = _STRING_LENGTH.unpack_from(view, offset)[0]
                if ordinal >= count or ordinal <= previous:
                    raise ValidationError("native semantic predecessor coordinate is invalid")
                previous = ordinal
                offset += 4
            flag = view[offset]
            offset += 1
            if flag not in (0, 1, 2) or source_count != predecessor_count + int(flag != 0) or (
                flag == 2 and predecessor_count != 0
            ) or (flag == 0 and predecessor_count == 0):
                raise ValidationError("native semantic launch flag disagrees")
        if offset != end:
            raise ValidationError("Static Exact cut-node stream has trailing data")
        return (
            net,
            cut_class,
            source_fpga,
            sink_fpgas,
            dependency_level,
            combinational_depth,
            predecessors,
            source_segments,
            capture_segments,
        )

    def decode(index: int) -> dict[str, Any]:
        (
            net,
            cut_class,
            source_fpga,
            sink_fpgas,
            dependency_level,
            combinational_depth,
            predecessors,
            source_segments,
            capture_segments,
        ) = decode_fields(index, raw=False)
        return {
            "net": net,
            "cut_class": cut_class,
            "source_fpgas": [source_fpga],
            "sink_fpgas": sink_fpgas,
            "dependency_level": dependency_level,
            "combinational_dependency_depth": combinational_depth,
            "predecessor_cut_nets": predecessors,
            "source_segment_ids": source_segments,
            "capture_segment_ids": capture_segments,
        }

    def cut_nodes() -> Iterator[dict[str, Any]]:
        for index in range(count):
            yield decode(index)

    result = StaticExactCutNodeSequence(count, cut_nodes, decode)
    result._bind_binary_decoder(lambda index: decode_fields(index, raw=True))
    return result


def build_static_exact_capture_stream(
    capture_relation: Iterable[tuple[Any, ...]],
) -> StaticExactCaptureSequence:
    """Pack the canonical native capture relation behind a typed record stream.

    Python resolves native capture coordinates against authoritative EmuIR.
    Production publication retains those records in one compact byte buffer
    instead of expanding a dictionary for every capture requirement.
    Diagnostic callers can still materialize the returned sequence with
    ``list(...)``.
    """

    encoded = bytearray()
    previous_key: tuple[Any, ...] | None = None
    count = 0
    for item in capture_relation:
        if not isinstance(item, tuple) or len(item) != 6:
            raise ValidationError("Static Exact capture relation is invalid")
        cut_net, fpga, kind, endpoint, port, bit = item
        if (
            not all(
                isinstance(value, str) and value
                for value in (cut_net, fpga, kind, endpoint)
            )
            or kind not in {"top-output", "architectural-state"}
            or (
                kind == "top-output"
                and (port is not None or bit is not None)
            )
            or (
                kind == "architectural-state"
                and (
                    not isinstance(port, str)
                    or not port
                )
            )
        ):
            raise ValidationError("Static Exact capture relation is invalid")
        if kind == "architectural-state":
            bit = validate_static_exact_capture_bit(bit)
        sort_key = (
            cut_net,
            fpga,
            kind,
            endpoint,
            "" if port is None else port,
            -1 if bit is None else bit,
        )
        if previous_key is not None and sort_key <= previous_key:
            raise ValidationError("Static Exact capture relation is invalid")
        previous_key = sort_key
        try:
            fields = [
                cut_net.encode("utf-8"),
                fpga.encode("utf-8"),
                endpoint.encode("utf-8"),
                b"" if port is None else port.encode("utf-8"),
                (
                    b""
                    if bit is None
                    else bit.to_bytes(max(1, (bit.bit_length() + 7) // 8), "big")
                ),
            ]
        except UnicodeEncodeError as error:
            raise ValidationError(
                "Static Exact capture identity is invalid"
            ) from error
        if any(len(field) >= 2**32 for field in fields):
            raise ValidationError("Static Exact capture identity is too large")
        encoded.extend(
            _CAPTURE_HEADER.pack(
                0 if kind == "top-output" else 1,
                *(len(field) for field in fields),
            )
        )
        for field in fields:
            encoded.extend(field)
        count += 1

    def binary_captures(
    ) -> Iterator[tuple[bytes, bytes, int, bytes, bytes, bytes]]:
        view = memoryview(encoded)
        offset = 0
        for _index in range(count):
            if offset + _CAPTURE_HEADER.size > len(view):
                raise ValidationError("Static Exact capture stream ended early")
            kind, *lengths = _CAPTURE_HEADER.unpack_from(view, offset)
            offset += _CAPTURE_HEADER.size
            fields: list[bytes] = []
            for length in lengths:
                end = offset + length
                if end > len(view):
                    raise ValidationError(
                        "Static Exact capture stream ended early"
                    )
                fields.append(bytes(view[offset:end]))
                offset = end
            if (
                kind not in {0, 1}
                or (kind == 0 and (fields[3] or fields[4]))
                or (
                    kind == 1
                    and (
                        not fields[3]
                        or not fields[4]
                        or (len(fields[4]) > 1 and fields[4][0] == 0)
                    )
                )
            ):
                raise ValidationError("Static Exact capture stream is invalid")
            for field in fields[:4]:
                try:
                    field.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValidationError(
                        "Static Exact capture stream identity is invalid"
                    ) from error
            yield fields[0], fields[1], kind, fields[2], fields[3], fields[4]
        if offset != len(view):
            raise ValidationError("Static Exact capture stream has trailing data")

    def capture_requirements() -> Iterator[dict[str, Any]]:
        for index, (
            cut_net,
            fpga,
            kind,
            endpoint,
            port,
            bit,
        ) in enumerate(binary_captures()):
            yield {
                "id": f"capture{index:06d}",
                "cut_net": cut_net.decode("utf-8"),
                "fpga": fpga.decode("utf-8"),
                "kind": "top-output" if kind == 0 else "architectural-state",
                "endpoint": endpoint.decode("utf-8"),
                **(
                    {"port": port.decode("utf-8"), "bit": int.from_bytes(bit, "big")}
                    if kind == 1
                    else {}
                ),
            }

    return StaticExactCaptureSequence(
        count, capture_requirements, binary_captures
    )


def read_native_static_exact_capture_stream(path: Path) -> StaticExactCaptureSequence:
    """Map native canonical captures with direct indexed access for validation."""

    try:
        with path.open("rb") as stream:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        if len(data) < 32:
            raise ValidationError("native capture header is truncated")
        magic, version, count, size = struct.unpack_from("<QIIQ", data)
        start = 24 + 8 * (count + 1)
        if magic != 0x31434553464D5545 or version != 1 or start + size != len(data):
            raise ValidationError("native capture envelope is invalid")
        offsets = memoryview(data)[24:start].cast("Q")
        if offsets[0] != 0 or offsets[-1] != size or any(
            offsets[index] >= offsets[index + 1] for index in range(count)
        ):
            raise ValidationError("native capture offsets are invalid")
        def binary(index):
            offset, end = start + offsets[index], start + offsets[index + 1]
            if offset + _CAPTURE_HEADER.size > end:
                raise ValidationError("native capture record is truncated")
            kind, *lengths = _CAPTURE_HEADER.unpack_from(data, offset)
            offset += _CAPTURE_HEADER.size
            if offset + sum(lengths) != end:
                raise ValidationError("native capture field lengths disagree")
            fields = []
            for length in lengths:
                fields.append(bytes(data[offset:offset + length]))
                offset += length
            if kind not in (0, 1) or any(not value for value in fields[:3]) or (
                kind == 0 and (fields[3] or fields[4])
            ) or (kind == 1 and (not fields[3] or not fields[4] or
                len(fields[4]) > STATIC_EXACT_CAPTURE_BIT_MAX_BYTES or
                (len(fields[4]) > 1 and fields[4][0] == 0))):
                raise ValidationError("native capture fields are invalid")
            try:
                for value in fields[:4]:
                    value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError("native capture identity is invalid") from error
            return fields[0], fields[1], kind, fields[2], fields[3], fields[4]
        def binaries():
            previous = None
            for index in range(count):
                row = binary(index)
                key = (row[0], row[1], b"top-output" if row[2] == 0 else b"architectural-state",
                       row[3], row[4], len(row[5]), row[5])
                if previous is not None and key <= previous:
                    raise ValidationError("native capture canonical order is invalid")
                previous = key
                yield row
        def records():
            for index, (net, fpga, kind, endpoint, port, bit) in enumerate(binaries()):
                yield {
                    "id": f"capture{index:06d}", "cut_net": net.decode("utf-8"),
                    "fpga": fpga.decode("utf-8"),
                    "kind": "top-output" if kind == 0 else "architectural-state",
                    "endpoint": endpoint.decode("utf-8"),
                    **({"port": port.decode("utf-8"), "bit": int.from_bytes(bit, "big")} if kind == 1 else {}),
                }
        result = StaticExactCaptureSequence(count, records, binaries)
        result._binary_indexer = binary
        result.native_capture_artifact_path = path
        return result
    except (OSError, ValueError, struct.error) as error:
        raise ValidationError("native capture artifact is invalid") from error


def build_static_exact_record_streams(
    cut_nodes: Sequence[Mapping[str, Any]],
    captures: Sequence[Mapping[str, Any]],
    launch_flags: bytes | bytearray,
    *,
    integer_topology: Any = None,
) -> dict[str, StaticExactRecordSequence]:
    """Build canonical edge/segment streams from final compact relations.

    ``launch_flags`` is aligned with ``cut_nodes``: zero means no local launch,
    one means an architectural launch, and two means a configuration-stable
    constant launch.  Segment identities already present in each cut node are
    the authority for ordering; every traversal checks their exact coverage.
    """

    if not isinstance(launch_flags, (bytes, bytearray)):
        raise ValidationError("Static Exact launch stream is invalid")
    # The derived factories and direct typed source must observe one immutable
    # generation even when a caller supplied a mutable bytearray.
    launch_flags = bytes(launch_flags)
    if len(launch_flags) != len(cut_nodes) or any(
        flag not in {0, 1, 2} for flag in launch_flags
    ):
        raise ValidationError("Static Exact launch stream is invalid")
    dependency_count = 0
    launch_count = 0
    capture_segment_count = 0
    for index, node in enumerate(cut_nodes):
        if not isinstance(node, Mapping):
            raise ValidationError("Static Exact cut-node stream is invalid")
        predecessors = node.get("predecessor_cut_nets")
        source_segments = node.get("source_segment_ids")
        capture_segments = node.get("capture_segment_ids")
        source_fpgas = node.get("source_fpgas")
        if (
            not isinstance(predecessors, list)
            or not all(
                isinstance(item, str) and item for item in predecessors
            )
            or not isinstance(source_segments, list)
            or not all(
                isinstance(item, str) and item for item in source_segments
            )
            or not isinstance(capture_segments, list)
            or not all(
                isinstance(item, str) and item for item in capture_segments
            )
            or not isinstance(source_fpgas, list)
            or len(source_fpgas) != 1
            or not isinstance(source_fpgas[0], str)
            or not source_fpgas[0]
            or len(source_segments)
            != len(predecessors) + int(launch_flags[index] != 0)
        ):
            raise ValidationError("Static Exact cut-node stream is invalid")
        dependency_count += len(predecessors)
        launch_count += int(launch_flags[index] != 0)
        capture_segment_count += len(capture_segments)
    if capture_segment_count != len(captures):
        raise ValidationError("Static Exact capture stream coverage disagrees")
    capture_segment_start = launch_count + dependency_count
    capture_index = 0
    capture_iterator = iter(captures)
    for node in cut_nodes:
        segment_ids = node["capture_segment_ids"]
        expected_ids = sorted(
            f"segment{capture_segment_start + index:06d}"
            for index in range(
                capture_index, capture_index + len(segment_ids)
            )
        )
        if segment_ids != expected_ids:
            raise ValidationError("Static Exact capture segment order disagrees")
        for _ in segment_ids:
            capture = next(capture_iterator, None)
            if (
                not isinstance(capture, Mapping)
                or capture.get("cut_net") != node.get("net")
            ):
                raise ValidationError(
                    "Static Exact capture stream order disagrees"
                )
            capture_index += 1
    if next(capture_iterator, None) is not None:
        raise ValidationError("Static Exact capture stream coverage disagrees")

    def dependency_edges() -> Iterator[dict[str, Any]]:
        observed = 0
        for index, node in enumerate(cut_nodes):
            predecessors = node["predecessor_cut_nets"]
            source_segments = node["source_segment_ids"]
            offset = int(launch_flags[index] != 0)
            for ordinal, source in enumerate(predecessors):
                observed += 1
                yield {
                    "from": source,
                    "to": node["net"],
                    "segment": source_segments[offset + ordinal],
                }
        if observed != dependency_count:
            raise ValidationError("Static Exact dependency stream ended early")

    def logic_segments() -> Iterator[dict[str, Any]]:
        observed = 0
        for index, node in enumerate(cut_nodes):
            flag = launch_flags[index]
            if flag:
                observed += 1
                yield {
                    "id": node["source_segment_ids"][0],
                    "kind": "launch_to_tx",
                    "fpga": node["source_fpgas"][0],
                    "sink_cut_net": node["net"],
                    **(
                        {
                            "source_semantics": (
                                "configuration-stable-constant"
                            )
                        }
                        if flag == 2
                        else {}
                    ),
                }
        for index, node in enumerate(cut_nodes):
            offset = int(launch_flags[index] != 0)
            for ordinal, source in enumerate(
                node["predecessor_cut_nets"]
            ):
                observed += 1
                yield {
                    "id": node["source_segment_ids"][offset + ordinal],
                    "kind": "rx_to_tx",
                    "fpga": node["source_fpgas"][0],
                    "source_cut_net": source,
                    "sink_cut_net": node["net"],
                }
        capture_index = 0
        for capture in captures:
            observed += 1
            yield {
                "id": f"segment{capture_segment_start + capture_index:06d}",
                "kind": "rx_to_capture",
                "fpga": capture["fpga"],
                "source_cut_net": capture["cut_net"],
                "capture_requirement": capture["id"],
            }
            capture_index += 1
        if (
            observed != launch_count + dependency_count + len(captures)
            or capture_index != len(captures)
        ):
            raise ValidationError("Static Exact logic stream ended early")

    result = {
        "dependency_edges": StaticExactRecordSequence(
            "dependency_edges", dependency_count, dependency_edges
        ),
        "logic_segments": StaticExactRecordSequence(
            "logic_segments",
            launch_count + dependency_count + len(captures),
            logic_segments,
        ),
    }
    if integer_topology is not None:
        if not isinstance(cut_nodes, StaticExactCutNodeSequence) or not isinstance(
            captures, StaticExactCaptureSequence
        ):
            raise ValidationError("Static Exact direct binary source is invalid")
        cut_nodes.bind_direct_binary_source(
            captures,
            launch_flags,
            integer_topology,
            result["dependency_edges"],
            result["logic_segments"],
            dependency_count,
        )
    return result


class _CanonicalDigestWriter:
    """Emit the exact ``json.dumps(indent=2, sort_keys=True)`` byte stream."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.parts: list[str] = []
        self.buffered_characters = 0

    def write(self, value: str) -> None:
        self.parts.append(value)
        self.buffered_characters += len(value)
        if self.buffered_characters >= 8 * 1024:
            self.flush()

    def flush(self) -> None:
        if self.parts:
            self.digest.update("".join(self.parts).encode("utf-8"))
            self.parts.clear()
            self.buffered_characters = 0

    def hexdigest(self) -> str:
        self.flush()
        return self.digest.hexdigest()

    def scalar(self, value: Any) -> None:
        try:
            if isinstance(value, str):
                self.write(encode_basestring_ascii(value))
            elif value is None:
                self.write("null")
            elif value is True:
                self.write("true")
            elif value is False:
                self.write("false")
            elif isinstance(value, int):
                self.write(str(value))
            elif isinstance(value, float):
                self.write(json.dumps(value))
            else:
                raise TypeError("unsupported canonical scalar")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ValidationError(
                "Static Exact canonical digest value is invalid"
            ) from error

    def value(self, value: Any, level: int) -> None:
        if isinstance(value, Mapping):
            self.mapping(value, level)
        elif isinstance(value, (list, tuple)):
            self.array(value, level)
        else:
            self.scalar(value)

    def mapping(self, value: Mapping[str, Any], level: int) -> None:
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise ValidationError(
                "Static Exact canonical digest mapping is invalid"
            )
        keys.sort()
        self.write("{")
        for index, key in enumerate(keys):
            self.write(("," if index else "") + "\n" + "  " * (level + 1))
            self.scalar(key)
            self.write(": ")
            self.value(value[key], level + 1)
        if keys:
            self.write("\n" + "  " * level)
        self.write("}")

    def array(self, value: Sequence[Any], level: int) -> None:
        self.write("[")
        for index, item in enumerate(value):
            self.write(("," if index else "") + "\n" + "  " * (level + 1))
            self.value(item, level + 1)
        if len(value):
            self.write("\n" + "  " * level)
        self.write("]")

    def object_fields(
        self, fields: Sequence[tuple[str, Any]], level: int
    ) -> None:
        if any(
            index and fields[index - 1][0] >= fields[index][0]
            for index in range(len(fields))
        ):
            raise ValidationError(
                "Static Exact canonical digest field order is invalid"
            )
        parts = ["{"]
        for index, (key, value) in enumerate(fields):
            parts.append(("," if index else "") + "\n" + "  " * (level + 1))
            parts.append(encode_basestring_ascii(key))
            parts.append(": ")
            parts.append(_canonical_direct_text(value, level + 1))
        if fields:
            parts.append("\n" + "  " * level)
        parts.append("}")
        self.write("".join(parts))


def _canonical_direct_text(value: Any, level: int) -> str:
    """Format one bounded raw record field without tiny digest updates."""

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "Static Exact canonical digest identity is invalid"
            ) from error
    if isinstance(value, str):
        return encode_basestring_ascii(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return (
            "[\n"
            + ",\n".join(
                "  " * (level + 1) + _canonical_direct_text(item, level + 1)
                for item in value
            )
            + "\n"
            + "  " * level
            + "]"
        )
    raise ValidationError("Static Exact canonical digest value is invalid")


def _direct_node(
    cut_nodes: StaticExactCutNodeSequence, index: int
) -> tuple[Any, ...]:
    node = cut_nodes.binary_node(index)
    if not isinstance(node, tuple) or len(node) != 9:
        raise ValidationError("Static Exact direct digest cut node is invalid")
    (
        net,
        cut_class,
        source_fpga,
        sink_fpgas,
        dependency_level,
        combinational_depth,
        predecessors,
        source_segments,
        capture_segments,
    ) = node
    byte_fields = (net, cut_class, source_fpga)
    byte_lists = (sink_fpgas, predecessors, source_segments, capture_segments)
    if (
        any(not isinstance(item, bytes) or not item for item in byte_fields)
        or any(
            not isinstance(items, list)
            or any(not isinstance(item, bytes) or not item for item in items)
            for items in byte_lists
        )
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value >= 2**32
            for value in (dependency_level, combinational_depth)
        )
    ):
        raise ValidationError("Static Exact direct digest cut node is invalid")
    return node


def _emit_direct_capture_requirements(
    writer: _CanonicalDigestWriter,
    source: StaticExactDirectBinarySource,
    level: int,
) -> None:
    writer.write("[")
    observed = 0
    for index, (cut_net, fpga, kind, endpoint, port, bit) in enumerate(
        source.captures.iter_binary_captures()
    ):
        writer.write(("," if index else "") + "\n" + "  " * (level + 1))
        if kind == 0:
            fields = (
                ("cut_net", cut_net),
                ("endpoint", endpoint),
                ("fpga", fpga),
                ("id", f"capture{index:06d}"),
                ("kind", "top-output"),
            )
        elif kind == 1 and port and bit:
            fields = (
                ("bit", int.from_bytes(bit, "big")),
                ("cut_net", cut_net),
                ("endpoint", endpoint),
                ("fpga", fpga),
                ("id", f"capture{index:06d}"),
                ("kind", "architectural-state"),
                ("port", port),
            )
        else:
            raise ValidationError("Static Exact direct digest capture is invalid")
        writer.object_fields(fields, level + 1)
        observed += 1
    if observed != len(source.captures):
        raise ValidationError("Static Exact direct digest capture ended early")
    if observed:
        writer.write("\n" + "  " * level)
    writer.write("]")


def _emit_direct_cut_nodes(
    writer: _CanonicalDigestWriter,
    source: StaticExactDirectBinarySource,
    level: int,
) -> None:
    writer.write("[")
    for index in range(len(source.cut_nodes)):
        node = _direct_node(source.cut_nodes, index)
        if len(node[7]) != len(node[6]) + int(source.launch_flags[index] != 0):
            raise ValidationError(
                "Static Exact direct digest source segment cardinality is invalid"
            )
        writer.write(("," if index else "") + "\n" + "  " * (level + 1))
        writer.object_fields(
            (
                ("capture_segment_ids", node[8]),
                ("combinational_dependency_depth", node[5]),
                ("cut_class", node[1]),
                ("dependency_level", node[4]),
                ("net", node[0]),
                ("predecessor_cut_nets", node[6]),
                ("sink_fpgas", node[3]),
                ("source_fpgas", (node[2],)),
                ("source_segment_ids", node[7]),
            ),
            level + 1,
        )
    if len(source.cut_nodes):
        writer.write("\n" + "  " * level)
    writer.write("]")


def _emit_direct_dependency_edges(
    writer: _CanonicalDigestWriter,
    source: StaticExactDirectBinarySource,
    level: int,
) -> None:
    writer.write("[")
    observed = 0
    for index in range(len(source.cut_nodes)):
        node = _direct_node(source.cut_nodes, index)
        offset = int(source.launch_flags[index] != 0)
        for ordinal, predecessor in enumerate(node[6]):
            writer.write(
                ("," if observed else "") + "\n" + "  " * (level + 1)
            )
            writer.object_fields(
                (
                    ("from", predecessor),
                    ("segment", node[7][offset + ordinal]),
                    ("to", node[0]),
                ),
                level + 1,
            )
            observed += 1
    if observed:
        writer.write("\n" + "  " * level)
    writer.write("]")


def _emit_direct_logic_segments(
    writer: _CanonicalDigestWriter,
    source: StaticExactDirectBinarySource,
    level: int,
    capture_start: int,
) -> None:
    writer.write("[")
    observed = 0
    for index in range(len(source.cut_nodes)):
        node = _direct_node(source.cut_nodes, index)
        flag = source.launch_flags[index]
        if not flag:
            continue
        fields: tuple[tuple[str, Any], ...] = (
            ("fpga", node[2]),
            ("id", node[7][0]),
            ("kind", "launch_to_tx"),
            ("sink_cut_net", node[0]),
        )
        if flag == 2:
            fields += (("source_semantics", "configuration-stable-constant"),)
        writer.write(("," if observed else "") + "\n" + "  " * (level + 1))
        writer.object_fields(fields, level + 1)
        observed += 1
    for index in range(len(source.cut_nodes)):
        node = _direct_node(source.cut_nodes, index)
        offset = int(source.launch_flags[index] != 0)
        for ordinal, predecessor in enumerate(node[6]):
            writer.write(
                ("," if observed else "") + "\n" + "  " * (level + 1)
            )
            writer.object_fields(
                (
                    ("fpga", node[2]),
                    ("id", node[7][offset + ordinal]),
                    ("kind", "rx_to_tx"),
                    ("sink_cut_net", node[0]),
                    ("source_cut_net", predecessor),
                ),
                level + 1,
            )
            observed += 1
    for capture_index, (cut_net, fpga, _kind, _endpoint, _port, _bit) in enumerate(
        source.captures.iter_binary_captures()
    ):
        writer.write(("," if observed else "") + "\n" + "  " * (level + 1))
        writer.object_fields(
            (
                ("capture_requirement", f"capture{capture_index:06d}"),
                ("fpga", fpga),
                ("id", f"segment{capture_start + capture_index:06d}"),
                ("kind", "rx_to_capture"),
                ("source_cut_net", cut_net),
            ),
            level + 1,
        )
        observed += 1
    if observed:
        writer.write("\n" + "  " * level)
    writer.write("]")


def _native_semantic_contract_sha256(value, source, keys, special_keys, paths) -> str:
    from .native_tools import resolve_native_stage_command

    tags = {"capture_requirements": 1, "cut_nodes": 2, "dependency_edges": 3, "logic_segments": 4}
    class HeaderText(_CanonicalDigestWriter):
        def __init__(self):
            self.parts = []
        def write(self, text):
            self.parts.append(text)
    with tempfile.TemporaryDirectory(prefix="emuflow-exact-hash-") as temporary:
        root = Path(temporary)
        request, output = root / "header.bin", root / "digest.txt"
        with request.open("wb") as stream:
            stream.write(struct.pack("<QII", 0x31484553464D5545, 1, len(keys)))
            def write_string(text):
                raw = text.encode("utf-8")
                if not raw or len(raw) >= 2**32:
                    raise ValidationError("native canonical header field is invalid")
                stream.write(struct.pack("<I", len(raw)))
                stream.write(raw)
            for key in keys:
                write_string(key)
                stream.write(struct.pack("<B", tags.get(key, 0)))
                if key not in special_keys:
                    # Header fields are bounded. Preserve the exact Python
                    # formatting contract while C++ walks the large relations.
                    formatted = HeaderText()
                    formatted.value(value[key], 1)
                    write_string("".join(formatted.parts))
        command = resolve_native_stage_command(
            "emuflow_static_exact_analyzer", "phase3", "hash-semantics"
        )
        if len(command) == 1:
            command = [*command, "--hash-semantics"]
        completed = subprocess.run(
            [*command, str(paths[0]), str(paths[1]), str(request), str(output)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            raise ValidationError("native canonical hashing failed: " + completed.stderr.strip())
        digest = output.read_text(encoding="ascii")
        if len(digest) != 65 or digest[-1] != "\n" or any(
            character not in "0123456789abcdef" for character in digest[:-1]
        ):
            raise ValidationError("native canonical digest is invalid")
        return digest[:-1]


def direct_static_exact_semantic_contract_sha256(
    value: Mapping[str, Any],
) -> str | None:
    """Hash a production contract from compact typed relations.

    ``None`` means the mapping is a legacy/materialized contract and should use
    the generic canonical encoder.  Once a direct source is attached, every
    logical array must be the exact sequence bound with it; a mixed generation
    fails closed instead of silently falling back to dictionary replay.
    """

    cut_nodes = value.get("cut_nodes")
    if not isinstance(cut_nodes, StaticExactCutNodeSequence):
        return None
    source = cut_nodes.direct_binary_source
    if source is None:
        return None
    captures = value.get("capture_requirements")
    dependency_edges = value.get("dependency_edges")
    logic_segments = value.get("logic_segments")
    if (
        captures is not source.captures
        or dependency_edges is not source.dependency_edges
        or logic_segments is not source.logic_segments
    ):
        raise ValidationError("Static Exact direct digest generation is mixed")
    capture_start = source.launch_count + source.dependency_count
    writer = _CanonicalDigestWriter()
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise ValidationError("Static Exact canonical digest mapping is invalid")
    keys.sort()
    # Direct formatting is defined only for the schema-valid JSON header.  Run
    # the standard encoder over those bounded fields first so unsupported
    # values (for example bytes or circular containers) retain the generic
    # canonical hasher's TypeError/ValueError behavior.
    standard_encoder = json.JSONEncoder(indent=2, sort_keys=True)
    special_keys = {
        "capture_requirements",
        "cut_nodes",
        "dependency_edges",
        "logic_segments",
    }
    for key in keys:
        if key in special_keys:
            continue
        for chunk in standard_encoder.iterencode(value[key]):
            chunk.encode("utf-8")
    native_paths = source.native_record_paths()
    if native_paths is not None:
        return _native_semantic_contract_sha256(value, source, keys, special_keys, native_paths)
    writer.write("{")
    for index, key in enumerate(keys):
        writer.write(("," if index else "") + "\n  ")
        writer.scalar(key)
        writer.write(": ")
        if key == "capture_requirements":
            _emit_direct_capture_requirements(writer, source, 1)
        elif key == "cut_nodes":
            _emit_direct_cut_nodes(writer, source, 1)
        elif key == "dependency_edges":
            _emit_direct_dependency_edges(writer, source, 1)
        elif key == "logic_segments":
            _emit_direct_logic_segments(writer, source, 1, capture_start)
        else:
            writer.value(value[key], 1)
    if keys:
        writer.write("\n")
    writer.write("}\n")
    return writer.hexdigest()


def is_static_exact_record_sequence(value: Any) -> bool:
    return isinstance(value, StaticExactRecordSequence)
