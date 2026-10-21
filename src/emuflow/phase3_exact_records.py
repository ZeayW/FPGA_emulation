"""Repeatable typed record streams for the Static Exact data plane.

The logical v3 semantic contract remains a mapping containing JSON arrays.
Production Phase 3, however, does not need to retain a Python dictionary for
every derived dependency edge and logic segment before publishing
``assignment.exact.bin``.  These read-only list subclasses generate those
records from the smaller cut-node/capture relations on each traversal, so the
canonical JSON hasher and exact-binary writer can consume the same ordered
record stream directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from .errors import ValidationError


STATIC_EXACT_RECORD_STREAM_SCHEMA = "emuflow.static-exact-record-stream/v1"


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
        for index in range(capture_index, capture_index + len(segment_ids)):
            capture = captures[index]
            if (
                not isinstance(capture, Mapping)
                or capture.get("cut_net") != node.get("net")
            ):
                raise ValidationError(
                    "Static Exact capture stream order disagrees"
                )
        capture_index += len(segment_ids)

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
