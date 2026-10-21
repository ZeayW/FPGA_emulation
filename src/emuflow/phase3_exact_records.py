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

import struct
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._indexer(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Static Exact record index must be an integer")
        resolved = index if index >= 0 else len(self) + index
        if resolved < 0 or resolved >= len(self):
            raise IndexError("Static Exact record index out of range")
        return self._indexer(resolved)


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

    count = len(offsets)

    def decode(index: int) -> dict[str, Any]:
        view = memoryview(encoded)
        offset = offsets[index]
        end = offsets[index + 1] if index + 1 < count else len(view)

        def read_string() -> str:
            nonlocal offset
            if offset + _STRING_LENGTH.size > end:
                raise ValidationError("Static Exact cut-node stream ended early")
            (length,) = _STRING_LENGTH.unpack_from(view, offset)
            offset += _STRING_LENGTH.size
            value_end = offset + length
            if value_end > end:
                raise ValidationError("Static Exact cut-node stream ended early")
            try:
                value = bytes(view[offset:value_end]).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError(
                    "Static Exact cut-node identity is invalid"
                ) from error
            offset = value_end
            if not value:
                raise ValidationError("Static Exact cut-node stream is invalid")
            return value

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

        result = {
            "net": net,
            "cut_class": cut_class,
            "source_fpgas": [source_fpga],
            "sink_fpgas": read_strings(sink_count),
            "dependency_level": dependency_level,
            "combinational_dependency_depth": combinational_depth,
            "predecessor_cut_nets": read_strings(predecessor_count),
            "source_segment_ids": read_strings(source_count),
            "capture_segment_ids": read_strings(capture_count),
        }
        if offset != end:
            raise ValidationError("Static Exact cut-node stream has trailing data")
        return result

    def cut_nodes() -> Iterator[dict[str, Any]]:
        for index in range(count):
            yield decode(index)

    return StaticExactCutNodeSequence(count, cut_nodes, decode)


def build_static_exact_capture_stream(
    capture_relation: Iterable[tuple[Any, ...]],
) -> StaticExactRecordSequence:
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

    def capture_requirements() -> Iterator[dict[str, Any]]:
        view = memoryview(encoded)
        offset = 0
        for index in range(count):
            if offset + _CAPTURE_HEADER.size > len(view):
                raise ValidationError("Static Exact capture stream ended early")
            kind, *lengths = _CAPTURE_HEADER.unpack_from(view, offset)
            offset += _CAPTURE_HEADER.size
            fields = []
            for field_index, length in enumerate(lengths):
                end = offset + length
                if end > len(view):
                    raise ValidationError(
                        "Static Exact capture stream ended early"
                    )
                raw = bytes(view[offset:end])
                if field_index == 4:
                    fields.append(raw)
                else:
                    try:
                        fields.append(raw.decode("utf-8"))
                    except UnicodeDecodeError as error:
                        raise ValidationError(
                            "Static Exact capture stream identity is invalid"
                        ) from error
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
            yield {
                "id": f"capture{index:06d}",
                "cut_net": fields[0],
                "fpga": fields[1],
                "kind": "top-output" if kind == 0 else "architectural-state",
                "endpoint": fields[2],
                **(
                    {"port": fields[3], "bit": int.from_bytes(fields[4], "big")}
                    if kind == 1
                    else {}
                ),
            }
        if offset != len(view):
            raise ValidationError("Static Exact capture stream has trailing data")

    return StaticExactRecordSequence(
        "capture_requirements", count, capture_requirements
    )


def build_static_exact_record_streams(
    cut_nodes: Sequence[Mapping[str, Any]],
    captures: Sequence[Mapping[str, Any]],
    launch_flags: bytes | bytearray,
) -> dict[str, StaticExactRecordSequence]:
    """Build canonical edge/segment streams from final compact relations.

    ``launch_flags`` is aligned with ``cut_nodes``: zero means no local launch,
    one means an architectural launch, and two means a configuration-stable
    constant launch.  Segment identities already present in each cut node are
    the authority for ordering; every traversal checks their exact coverage.
    """

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

    return {
        "dependency_edges": StaticExactRecordSequence(
            "dependency_edges", dependency_count, dependency_edges
        ),
        "logic_segments": StaticExactRecordSequence(
            "logic_segments",
            launch_count + dependency_count + len(captures),
            logic_segments,
        ),
    }


def is_static_exact_record_sequence(value: Any) -> bool:
    return isinstance(value, StaticExactRecordSequence)
