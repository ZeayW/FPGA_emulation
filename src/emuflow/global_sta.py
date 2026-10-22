"""Fixed-event timing binding for an external OpenSTA engine.

This is a timing abstraction, not a synthesizable transport implementation.
An event cutpoint has an absolute launch time, a chain of measured arcs, and
an explicit capture deadline. OpenSTA propagates the arcs and checks the
deadline. TX readiness checks MUST accompany terminal path observations:
observing the last TX alone cannot prove that it transported the current value.
No Python-computed arrival, slack, or TDM wait enters the exported circuit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from array import array
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .native_tools import resolve_native_executable
from .opensta import render_opensta_liberty, require_opensta_engine


@dataclass(frozen=True)
class EventCheck:
    path: str
    role: str
    event: str
    launch_ns: float
    arcs_ns: tuple[float, ...]
    required_ns: float


class _IndexedMeasurements(Sequence[Mapping[str, object]]):
    """Fixed-width OpenSTA scalar results over an existing check sequence.

    Check identity already belongs to ``rows``.  Retaining it again in one
    Python dictionary per measurement used substantially more memory than the
    three engine scalars, especially for saved-result validation where every
    original TX/commit/check row is read at once.  This view keeps exactly
    three doubles per row and materializes a mapping only for the row currently
    consumed by stitching or validation.
    """

    def __init__(
        self,
        rows: Sequence[EventCheck],
        arrivals: array,
        required: array,
        slacks: array,
    ):
        if (
            arrivals.typecode != "d"
            or required.typecode != "d"
            or slacks.typecode != "d"
            or len(arrivals) != len(rows)
            or len(required) != len(rows)
            or len(slacks) != len(rows)
        ):
            raise ValidationError("global STA indexed measurements are malformed")
        self._rows = rows
        self._arrivals = arrivals
        self._required = required
        self._slacks = slacks

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        row = self._rows[index]
        return {
            "path": row.path,
            "role": row.role,
            "event": row.event,
            "arrival_ns": self._arrivals[index],
            "required_ns": self._required[index],
            "slack_ns": self._slacks[index],
        }

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    @property
    def typed_payload_bytes(self) -> int:
        return sum(
            values.itemsize * len(values)
            for values in (self._arrivals, self._required, self._slacks)
        )


_EVENT_CHECK_ROLES = ("target", "runtime", "tx", "commit")
_EVENT_CHECK_ROLE_INDEX = {
    role: index for index, role in enumerate(_EVENT_CHECK_ROLES)
}
_UINT32_MAX = (1 << 32) - 1
_UINT64_MASK = (1 << 64) - 1


class _IndexedPathMetadata(Mapping[str, Mapping[str, object]]):
    """Sorted, fixed-width metadata for every original timing path.

    The production binding used to retain one Python dictionary for every
    original path in addition to the indexed event/check relation.  This view
    owns path IDs once, interns repeated metadata strings, and resolves a path
    with binary search.  Materialized dictionaries exist only for the current
    report row.
    """

    def __init__(
        self,
        paths: list[str],
        strings: list[str],
        representative_indices: array,
        scopes: array,
        clock_indices: array,
        target_periods: array,
        cone_bounds: array,
        model_indices: array,
    ):
        count = len(paths)
        if (
            len(representative_indices) != count
            or len(scopes) != count
            or len(clock_indices) != count
            or len(target_periods) != count
            or len(cone_bounds) != count
            or len(model_indices) != count
            or any(not path for path in paths)
            or any(paths[index - 1] >= paths[index] for index in range(1, count))
        ):
            raise ValidationError("global STA indexed path metadata is malformed")
        self._paths = paths
        self._strings = strings
        self._representative_indices = representative_indices
        self._scopes = scopes
        self._clock_indices = clock_indices
        self._target_periods = target_periods
        self._cone_bounds = cone_bounds
        self._model_indices = model_indices

    def __len__(self) -> int:
        return len(self._paths)

    def __iter__(self):
        return iter(self._paths)

    def __getitem__(self, path: str) -> Mapping[str, object]:
        if not isinstance(path, str):
            raise KeyError(path)
        index = bisect_left(self._paths, path)
        if index >= len(self._paths) or self._paths[index] != path:
            raise KeyError(path)
        representative_index = self._representative_indices[index]
        clock_index = self._clock_indices[index]
        model_index = self._model_indices[index]
        if (
            representative_index >= len(self._strings)
            or self._scopes[index] > 1
            or (
                clock_index != _UINT32_MAX
                and clock_index >= len(self._strings)
            )
            or self._cone_bounds[index] > 1
            or (
                model_index != _UINT32_MAX
                and model_index >= len(self._strings)
            )
        ):
            raise ValidationError(
                "global STA indexed path metadata identity is malformed"
            )
        target_period = self._target_periods[index]
        if not math.isfinite(target_period) or target_period <= 0.0:
            raise ValidationError(
                "global STA indexed path metadata period is malformed"
            )
        return {
            "representative_path": self._strings[representative_index],
            "path_scope": (
                "same-fpga-local" if self._scopes[index] else "cross-fpga"
            ),
            "clock_domain": (
                None
                if clock_index == _UINT32_MAX
                else self._strings[clock_index]
            ),
            "target_period_ns": target_period,
            "physical_logic_segments_cone_bound": bool(
                self._cone_bounds[index]
            ),
            **(
                {}
                if model_index == _UINT32_MAX
                else {
                    "interconnect_delay_model": self._strings[model_index]
                }
            ),
        }

    @property
    def typed_payload_bytes(self) -> int:
        """Bytes in fixed-width columns, excluding shared string objects."""

        return sum(
            values.itemsize * len(values)
            for values in (
                self._representative_indices,
                self._scopes,
                self._clock_indices,
                self._target_periods,
                self._cone_bounds,
                self._model_indices,
            )
        )

    def path_id_set_sha256(self) -> str:
        """Hash the already sorted unique path IDs without another copy/sort."""

        encoded = json.dumps(
            self._paths,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class _IndexedSystemTimingPaths(Sequence[Mapping[str, object]]):
    """Fixed-width final OpenSTA values over indexed original-path metadata."""

    def __init__(
        self,
        metadata: _IndexedPathMetadata,
        system_delays: array,
        target_required: array,
        target_slacks: array,
        runtime_required: array,
        runtime_slacks: array,
    ):
        values = (
            system_delays,
            target_required,
            target_slacks,
            runtime_required,
            runtime_slacks,
        )
        if any(column.typecode != "d" or len(column) != len(metadata) for column in values):
            raise ValidationError("global STA indexed timing paths are malformed")
        self._metadata = metadata
        self._system_delays = system_delays
        self._target_required = target_required
        self._target_slacks = target_slacks
        self._runtime_required = runtime_required
        self._runtime_slacks = runtime_slacks

    def __len__(self) -> int:
        return len(self._metadata)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        path = self._metadata._paths[index]
        return {
            "path": path,
            **self._metadata[path],
            "system_delay_bound_ns": self._system_delays[index],
            "target_required_time_ns": self._target_required[index],
            "target_clock_slack_bound_ns": self._target_slacks[index],
            "runtime_required_time_ns": self._runtime_required[index],
            "runtime_clock_slack_bound_ns": self._runtime_slacks[index],
        }

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    @property
    def typed_payload_bytes(self) -> int:
        return sum(
            column.itemsize * len(column)
            for column in (
                self._system_delays,
                self._target_required,
                self._target_slacks,
                self._runtime_required,
                self._runtime_slacks,
            )
        )


class _SystemTimingPathBuilder:
    """Place one complete measurement bundle into sorted metadata columns."""

    def __init__(self, metadata: _IndexedPathMetadata):
        self._metadata = metadata
        count = len(metadata)
        self._seen = array("B", [0]) * count
        self._system_delays = array("d", [math.nan]) * count
        self._target_required = array("d", [math.nan]) * count
        self._target_slacks = array("d", [math.nan]) * count
        self._runtime_required = array("d", [math.nan]) * count
        self._runtime_slacks = array("d", [math.nan]) * count

    def append(self, path: str, target: Mapping, runtime: Mapping) -> bool:
        index = bisect_left(self._metadata._paths, path)
        if index >= len(self._metadata) or self._metadata._paths[index] != path:
            raise ValidationError("global STA measurement path is unknown")
        if self._seen[index]:
            return False
        self._seen[index] = 1
        self._system_delays[index] = target["arrival_ns"]
        self._target_required[index] = target["required_ns"]
        self._target_slacks[index] = target["slack_ns"]
        self._runtime_required[index] = runtime["required_ns"]
        self._runtime_slacks[index] = runtime["slack_ns"]
        return True

    def finish(self) -> _IndexedSystemTimingPaths:
        if not self._seen or any(not value for value in self._seen):
            raise ValidationError(
                "global STA invalid standalone measurement population"
            )
        return _IndexedSystemTimingPaths(
            self._metadata,
            self._system_delays,
            self._target_required,
            self._target_slacks,
            self._runtime_required,
            self._runtime_slacks,
        )


class _PathMetadataBuilder:
    """Append path metadata, then seal it into sorted indexed columns."""

    def __init__(self):
        self._finished = False
        self._paths: list[str] = []
        self._strings: list[str] = []
        self._string_indices: dict[str, int] = {}
        self._representative_indices = array("I")
        self._scopes = array("B")
        self._clock_indices = array("I")
        self._target_periods = array("d")
        self._cone_bounds = array("B")
        self._model_indices = array("I")

    def _intern(self, value: str | None) -> int:
        if value is None:
            return _UINT32_MAX
        if not isinstance(value, str) or not value:
            raise ValidationError("global STA path metadata string is invalid")
        index = self._string_indices.get(value)
        if index is None:
            index = len(self._strings)
            if index >= _UINT32_MAX:
                raise ValidationError(
                    "global STA path metadata string population is too large"
                )
            self._string_indices[value] = index
            self._strings.append(value)
        return index

    def append(
        self,
        path: str,
        *,
        representative_path: str,
        path_scope: str,
        clock_domain: str | None,
        target_period_ns: float,
        physical_logic_segments_cone_bound: bool,
        interconnect_delay_model: str | None = None,
    ) -> None:
        if self._finished:
            raise ValidationError("global STA path metadata builder is sealed")
        if not isinstance(path, str) or not path:
            raise ValidationError("global STA path metadata identity is invalid")
        if (
            not isinstance(representative_path, str)
            or not representative_path
        ):
            raise ValidationError(
                "global STA path metadata representative is invalid"
            )
        if path_scope not in {"cross-fpga", "same-fpga-local"}:
            raise ValidationError("global STA path metadata scope is invalid")
        if (
            isinstance(target_period_ns, bool)
            or not isinstance(target_period_ns, (int, float))
            or not math.isfinite(float(target_period_ns))
            or float(target_period_ns) <= 0.0
            or not isinstance(physical_logic_segments_cone_bound, bool)
        ):
            raise ValidationError("global STA path metadata values are invalid")
        self._paths.append(path)
        self._representative_indices.append(self._intern(representative_path))
        self._scopes.append(path_scope == "same-fpga-local")
        self._clock_indices.append(self._intern(clock_domain))
        self._target_periods.append(float(target_period_ns))
        self._cone_bounds.append(physical_logic_segments_cone_bound)
        self._model_indices.append(self._intern(interconnect_delay_model))

    def finish(self) -> _IndexedPathMetadata:
        if self._finished:
            raise ValidationError("global STA path metadata builder is sealed")
        self._finished = True
        order = sorted(range(len(self._paths)), key=self._paths.__getitem__)
        paths = [self._paths[index] for index in order]
        if any(paths[index - 1] == paths[index] for index in range(1, len(paths))):
            raise ValidationError("global STA local/crossing path overlap")

        def reordered(values: array) -> array:
            return array(values.typecode, (values[index] for index in order))

        result = _IndexedPathMetadata(
            paths,
            self._strings,
            reordered(self._representative_indices),
            reordered(self._scopes),
            reordered(self._clock_indices),
            reordered(self._target_periods),
            reordered(self._cone_bounds),
            reordered(self._model_indices),
        )
        self._paths = []
        self._strings = []
        self._string_indices = {}
        return result


def _event_check_values_hash(
    role: str,
    launch_ns: float,
    arcs_ns: tuple[float, ...],
    required_ns: float,
) -> int:
    """Return a compact candidate key; callers still compare exact values."""

    return hash((role, launch_ns, arcs_ns, required_ns)) & _UINT64_MASK


def _event_check_row_hash(row: EventCheck) -> int:
    return _event_check_values_hash(
        row.role, row.launch_ns, row.arcs_ns, row.required_ns
    )


class _RepresentativeBundle:
    __slots__ = ("start", "count", "numerical_key", "collision")

    def __init__(
        self,
        start: int,
        count: int,
        numerical_key: tuple | None,
        collision: _RepresentativeBundle | None = None,
    ):
        self.start = start
        self.count = count
        self.numerical_key = numerical_key
        self.collision = collision


class _IndexedEventChecks(Sequence[EventCheck]):
    """Fixed-width, string-interned storage for the production check stream.

    A transported original path produces several TX/commit checks in addition
    to its target/runtime observations.  Keeping each row as a Python
    dataclass with a separate arc tuple made that hop-sized relation one of the
    last large in-process Global STA populations.  This view owns each path and
    event string once and stores the relation and arc payload in typed arrays.
    ``EventCheck`` objects are materialized only for the row currently being
    exported, validated, or stitched.
    """

    def __init__(
        self,
        paths: list[str],
        events: list[str],
        path_indices: array,
        roles: array,
        event_indices: array,
        launches: array,
        required: array,
        arc_offsets: array,
        arcs: array,
        numerical_hashes: array,
        original_to_representative: array | None = None,
        representative_indices: array | None = None,
        *,
        validated: bool = False,
    ):
        count = len(roles)
        if (
            len(path_indices) != count
            or len(event_indices) != count
            or len(launches) != count
            or len(required) != count
            or len(numerical_hashes) != count
            or (
                original_to_representative is not None
                and len(original_to_representative) != count
            )
            or (original_to_representative is None)
            != (representative_indices is None)
            or len(arc_offsets) != count + 1
            or arc_offsets[0] != 0
            or arc_offsets[-1] != len(arcs)
        ):
            raise ValidationError("global STA indexed check storage is malformed")
        self._paths = paths
        self._events = events
        self._path_indices = path_indices
        self._roles = roles
        self._event_indices = event_indices
        self._launches = launches
        self._required = required
        self._arc_offsets = arc_offsets
        self._arcs = arcs
        self._numerical_hashes = numerical_hashes
        self._original_to_representative = original_to_representative
        self._representative_indices = representative_indices
        self._validated = validated

    def __len__(self) -> int:
        return len(self._roles)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        path_index = self._path_indices[index]
        event_index = self._event_indices[index]
        role_index = self._roles[index]
        if (
            path_index >= len(self._paths)
            or event_index >= len(self._events)
            or role_index >= len(_EVENT_CHECK_ROLES)
        ):
            raise ValidationError("global STA indexed check identity is malformed")
        begin = self._arc_offsets[index]
        end = self._arc_offsets[index + 1]
        if begin > end or end > len(self._arcs):
            raise ValidationError("global STA indexed check arcs are malformed")
        return EventCheck(
            self._paths[path_index],
            _EVENT_CHECK_ROLES[role_index],
            self._events[event_index],
            self._launches[index],
            tuple(self._arcs[begin:end]),
            self._required[index],
        )

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    def _numerical_hash(self, index: int) -> int:
        return self._numerical_hashes[index]

    def _numerical_key(self, index: int) -> tuple:
        begin = self._arc_offsets[index]
        end = self._arc_offsets[index + 1]
        return (
            self._roles[index],
            self._launches[index],
            tuple(self._arcs[begin:end]),
            self._required[index],
        )

    def _validate_row(self, index: int) -> None:
        path_index = self._path_indices[index]
        event_index = self._event_indices[index]
        role_index = self._roles[index]
        if (
            path_index >= len(self._paths)
            or event_index >= len(self._events)
            or role_index >= len(_EVENT_CHECK_ROLES)
            or not self._paths[path_index]
            or not self._events[event_index]
        ):
            raise ValidationError("global STA indexed check identity is malformed")
        begin = self._arc_offsets[index]
        end = self._arc_offsets[index + 1]
        if begin > end or end > len(self._arcs):
            raise ValidationError("global STA indexed check arcs are malformed")
        launch = self._launches[index]
        required = self._required[index]
        if (
            not math.isfinite(launch)
            or not math.isfinite(required)
        ):
            raise ValidationError("global STA times must be finite numbers")
        if launch < 0:
            raise ValidationError(
                "global STA launch/arc delay must be nonnegative"
            )
        for offset in range(begin, end):
            value = self._arcs[offset]
            if not math.isfinite(value):
                raise ValidationError("global STA times must be finite numbers")
            if value < 0:
                raise ValidationError(
                    "global STA launch/arc delay must be nonnegative"
                )

    def _physical_chain_equal(self, left: int, right: int) -> bool:
        if self._launches[left] != self._launches[right]:
            return False
        left_begin = self._arc_offsets[left]
        left_end = self._arc_offsets[left + 1]
        right_begin = self._arc_offsets[right]
        right_end = self._arc_offsets[right + 1]
        return (
            left_end - left_begin == right_end - right_begin
            and all(
                self._arcs[left_begin + offset]
                == self._arcs[right_begin + offset]
                for offset in range(left_end - left_begin)
            )
        )

    @property
    def typed_payload_bytes(self) -> int:
        """Bytes in fixed-width relation buffers, excluding interned strings."""

        return sum(
            values.itemsize * len(values)
            for values in (
                self._path_indices,
                self._roles,
                self._event_indices,
                self._launches,
                self._required,
                self._arc_offsets,
                self._arcs,
                self._numerical_hashes,
                *(
                    (self._original_to_representative,)
                    if self._original_to_representative is not None
                    else ()
                ),
                *(
                    (self._representative_indices,)
                    if self._representative_indices is not None
                    else ()
                ),
            )
        )


class _EventCheckBuilder:
    """Append-only builder which releases its string dictionaries at seal."""

    def __init__(self):
        self._finished = False
        self._paths: list[str] = []
        self._events: list[str] = []
        self._path_ids: dict[str, int] = {}
        self._event_ids: dict[str, int] = {}
        self._path_indices = array("I")
        self._roles = array("B")
        self._event_indices = array("I")
        self._launches = array("d")
        self._required = array("d")
        self._arc_offsets = array("Q", [0])
        self._arcs = array("d")
        self._numerical_hashes = array("Q")
        self._original_to_representative = array("I")
        self._representative_indices = array("I")
        self._representative_bundles: dict[int, _RepresentativeBundle] = {}
        self._current_path_index: int | None = None
        self._current_bundle_start = 0
        self._current_identities: set[tuple[int, int]] = set()
        self._target_chain: tuple[float, tuple[float, ...]] | None = None
        self._runtime_chain: tuple[float, tuple[float, ...]] | None = None

    def _finish_bundle(self) -> None:
        if self._current_path_index is None:
            return
        if self._target_chain is None or self._runtime_chain is None:
            raise ValidationError("global STA target/runtime population disagrees")
        if self._target_chain != self._runtime_chain:
            raise ValidationError(
                "global STA target/runtime physical chains disagree"
            )
        begin = self._current_bundle_start
        end = len(self._roles)
        bundle_hash = end - begin
        for index in range(begin, end):
            bundle_hash = (
                (bundle_hash * 1_000_003) ^ self._numerical_hashes[index]
            ) & _UINT64_MASK

        def numerical_key(index: int) -> tuple:
            arc_begin = self._arc_offsets[index]
            arc_end = self._arc_offsets[index + 1]
            return (
                self._roles[index],
                self._launches[index],
                tuple(self._arcs[arc_begin:arc_end]),
                self._required[index],
            )

        start = None
        current_key = None
        candidate = self._representative_bundles.get(bundle_hash)
        while candidate is not None:
            if candidate.count != end - begin:
                candidate = candidate.collision
                continue
            if candidate.numerical_key is None:
                representative_key = tuple(
                    numerical_key(
                        self._representative_indices[
                            candidate.start + offset
                        ]
                    )
                    for offset in range(candidate.count)
                )
                candidate.numerical_key = representative_key
            else:
                representative_key = candidate.numerical_key
            if current_key is None:
                current_key = tuple(
                    numerical_key(index) for index in range(begin, end)
                )
            if current_key == representative_key:
                start = candidate.start
                break
            candidate = candidate.collision
        if start is None:
            start = len(self._representative_indices)
            if start + end - begin >= _UINT32_MAX:
                raise ValidationError(
                    "global STA representative population exceeds indexed storage"
                )
            self._representative_indices.extend(range(begin, end))
            self._representative_bundles[bundle_hash] = _RepresentativeBundle(
                start,
                end - begin,
                current_key,
                self._representative_bundles.get(bundle_hash),
            )
        for offset, index in enumerate(range(begin, end)):
            self._original_to_representative[index] = start + offset

    @staticmethod
    def _intern(value: str, values: list[str], ids: dict[str, int]) -> int:
        identity = ids.get(value)
        if identity is None:
            identity = len(values)
            if identity >= _UINT32_MAX:
                raise ValidationError("global STA indexed identity population is too large")
            ids[value] = identity
            values.append(value)
        return identity

    def append(self, row: EventCheck) -> None:
        self.append_values(
            row.path,
            row.role,
            row.event,
            row.launch_ns,
            row.arcs_ns,
            row.required_ns,
        )

    def append_values(
        self,
        path: str,
        role: str,
        event: str,
        launch_ns: float,
        arcs_ns: tuple[float, ...],
        required_ns: float,
    ) -> None:
        if self._finished:
            raise ValidationError("global STA indexed check builder is sealed")
        if not path or not event:
            raise ValidationError("global STA has duplicate/empty check identity")
        if role not in _EVENT_CHECK_ROLE_INDEX:
            raise ValidationError("global STA has an unsupported check role")
        for value in (launch_ns, required_ns, *arcs_ns):
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValidationError("global STA times must be finite numbers")
        if launch_ns < 0 or any(value < 0 for value in arcs_ns):
            raise ValidationError(
                "global STA launch/arc delay must be nonnegative"
            )
        path_index = self._intern(path, self._paths, self._path_ids)
        event_index = self._intern(event, self._events, self._event_ids)
        role_index = _EVENT_CHECK_ROLE_INDEX[role]
        if path_index != self._current_path_index:
            self._finish_bundle()
            # Dense IDs are assigned on first sight. Any non-latest ID here is
            # a reappearing path bundle, which the production builder rejects;
            # public list callers retain the legacy interleaved fallback.
            if path_index != len(self._paths) - 1:
                raise ValidationError(
                    "global STA indexed checks are not path-contiguous"
                )
            self._current_path_index = path_index
            self._current_identities = set()
            self._target_chain = None
            self._runtime_chain = None
            self._current_bundle_start = len(self._roles)
        identity = (role_index, event_index)
        if identity in self._current_identities:
            raise ValidationError("global STA has duplicate/empty check identity")
        self._current_identities.add(identity)
        if role == "target":
            if self._target_chain is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            self._target_chain = (launch_ns, arcs_ns)
        elif role == "runtime":
            if self._runtime_chain is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            self._runtime_chain = (launch_ns, arcs_ns)
        self._path_indices.append(path_index)
        self._roles.append(role_index)
        self._event_indices.append(event_index)
        self._launches.append(launch_ns)
        self._required.append(required_ns)
        self._arcs.extend(arcs_ns)
        self._arc_offsets.append(len(self._arcs))
        self._numerical_hashes.append(
            _event_check_values_hash(role, launch_ns, arcs_ns, required_ns)
        )
        self._original_to_representative.append(_UINT32_MAX)

    def finish(self) -> _IndexedEventChecks:
        if self._finished:
            raise ValidationError("global STA indexed check builder is sealed")
        self._finish_bundle()
        self._finished = True
        result = _IndexedEventChecks(
            self._paths,
            self._events,
            self._path_indices,
            self._roles,
            self._event_indices,
            self._launches,
            self._required,
            self._arc_offsets,
            self._arcs,
            self._numerical_hashes,
            self._original_to_representative,
            self._representative_indices,
            validated=True,
        )
        # These dictionaries are required only while assigning dense IDs.
        # The sealed view retains the ordered string pools alone.
        self._path_ids = {}
        self._event_ids = {}
        self._representative_bundles = {}
        return result


class _IndexedCheckSelection(Sequence[EventCheck]):
    """A fixed-width row selection over another check sequence."""

    def __init__(self, rows: Sequence[EventCheck], indices: array):
        self._rows = rows
        self._indices = indices

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        selected = self._indices[index]
        if selected >= len(self._rows):
            raise ValidationError("global STA representative index is malformed")
        return self._rows[selected]

    def __iter__(self):
        for index in self._indices:
            if index >= len(self._rows):
                raise ValidationError("global STA representative index is malformed")
            yield self._rows[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))


def _validate_check_values(row: EventCheck) -> None:
    if not row.path or not row.event:
        raise ValidationError("global STA has duplicate/empty check identity")
    if row.role not in {"target", "runtime", "tx", "commit"}:
        raise ValidationError("global STA has an unsupported check role")
    for value in (row.launch_ns, row.required_ns, *row.arcs_ns):
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValidationError("global STA times must be finite numbers")
    if row.launch_ns < 0 or any(x < 0 for x in row.arcs_ns):
        raise ValidationError("global STA launch/arc delay must be nonnegative")


def _validate_interleaved_checks(rows: Sequence[EventCheck]) -> None:
    """Compatibility validator for callers that interleave path bundles."""
    seen = set()
    observations: dict[str, list[EventCheck | None]] = {}
    for row in rows:
        _validate_check_values(row)
        key = (row.path, row.role, row.event)
        if key in seen:
            raise ValidationError("global STA has duplicate/empty check identity")
        seen.add(key)
        if row.role in {"target", "runtime"}:
            pair = observations.setdefault(row.path, [None, None])
            position = 0 if row.role == "target" else 1
            if pair[position] is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            pair[position] = row
    if not observations or any(
        target is None or runtime is None
        for target, runtime in observations.values()
    ):
        raise ValidationError("global STA target/runtime population disagrees")
    for row in rows:
        if row.path not in observations:
            raise ValidationError("global STA event has no original-path observation")
    for target, runtime in observations.values():
        assert target is not None and runtime is not None
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")


def _validate_indexed_checks(rows: _IndexedEventChecks) -> None:
    """Validate canonical typed rows without reconstructing every dataclass."""

    completed_paths: set[int] = set()
    current_path = None
    current_identities: set[tuple[int, int]] = set()
    target = None
    runtime = None
    interleaved = False

    def finish_bundle() -> bool:
        if current_path is None:
            return True
        if target is None or runtime is None:
            return False
        if not rows._physical_chain_equal(target, runtime):
            raise ValidationError(
                "global STA target/runtime physical chains disagree"
            )
        completed_paths.add(current_path)
        return True

    for index in range(len(rows)):
        rows._validate_row(index)
        path = rows._path_indices[index]
        if path != current_path:
            if not finish_bundle() or path in completed_paths:
                interleaved = True
                break
            current_path = path
            current_identities = set()
            target = None
            runtime = None
        role = rows._roles[index]
        identity = (role, rows._event_indices[index])
        if identity in current_identities:
            raise ValidationError("global STA has duplicate/empty check identity")
        current_identities.add(identity)
        if role == _EVENT_CHECK_ROLE_INDEX["target"]:
            if target is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            target = index
        elif role == _EVENT_CHECK_ROLE_INDEX["runtime"]:
            if runtime is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            runtime = index
    if not interleaved and not finish_bundle():
        interleaved = True
    if interleaved:
        _validate_interleaved_checks(rows)


def validate_checks(checks: Iterable[EventCheck]) -> Sequence[EventCheck]:
    # Preserve the public API's historical snapshot contract for every
    # caller-owned container, including list, tuple, and custom Sequence
    # implementations. Only the two private immutable/indexed views may stay
    # zero-copy across validation and export.
    rows = (
        checks
        if isinstance(checks, (_IndexedEventChecks, _IndexedCheckSelection))
        else list(checks)
    )
    if not rows:
        raise ValidationError("global STA has no timing checks")
    if isinstance(rows, _IndexedEventChecks):
        if not rows._validated:
            _validate_indexed_checks(rows)
        return rows

    # bind_physical_checks emits one contiguous bundle per original path.
    # Validate that order while retaining only the live bundle's event
    # identities and observation pair.  This removes the check-sized identity
    # set and path-sized observation table from the production data plane.
    # External callers that interleave bundles retain the original behavior
    # through the compatibility validator below.
    completed_paths = set()
    current_path = None
    current_identities = set()
    target = None
    runtime = None
    interleaved = False

    def finish_bundle() -> bool:
        if current_path is None:
            return True
        if target is None or runtime is None:
            return False
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")
        completed_paths.add(current_path)
        return True

    for row in rows:
        _validate_check_values(row)
        if row.path != current_path:
            if not finish_bundle() or row.path in completed_paths:
                interleaved = True
                break
            current_path = row.path
            current_identities = set()
            target = None
            runtime = None
        identity = (row.role, row.event)
        if identity in current_identities:
            raise ValidationError("global STA has duplicate/empty check identity")
        current_identities.add(identity)
        if row.role == "target":
            if target is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            target = row
        elif row.role == "runtime":
            if runtime is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            runtime = row
    if not interleaved and not finish_bundle():
        interleaved = True
    if interleaved:
        _validate_interleaved_checks(rows)
    return rows


def export_event_checks(checks: Iterable[EventCheck], directory: Path) -> Sequence[EventCheck]:
    """Export Verilog/Liberty/SDC and one batch query; names never contain user text.

Fixed-edge register cutpoints lower to SDC input launch times and output
deadlines relative to one epoch clock. Unlike periodic generated clocks this
cannot silently wrap a missed slot into the following frame. The measured arc
chain remains explicit; a scalar Liberty cell is shared for each unique delay.
"""
    rows = validate_checks(checks)
    directory.mkdir(parents=True, exist_ok=True)
    delays = sorted({0.0, *(v for r in rows for v in r.arcs_ns)})
    cells = {v: f"D{i}" for i, v in enumerate(delays)}
    model = {"name": "emuflow_event_arcs", "cells": {
        name: {"kind": "combinational", "inputs": ["A"], "output": "Y",
               "delay_ns": value} for value, name in cells.items()}}
    (directory / "global_timing.lib").write_text(render_opensta_liberty(model))
    # Translate each independent check to its launch epoch. A global, very
    # long frame clock loses precision when subtracted from short deadlines.
    # Fixed I/O constraints allow negative output delays without edge wrap.
    epoch = 1.0
    with (directory / "global_timing.v").open("w") as v, (directory / "global_timing.sdc").open("w") as s:
        v.write("module global_timing(\n" + ",\n".join(
            f"i{i}, o{i}" for i in range(len(rows))) + ");\n")
        for i in range(len(rows)):
            v.write(f"input i{i};\noutput o{i};\n")
        s.write(f"create_clock -name epoch -period {epoch:.17g}\n")
        for i, row in enumerate(rows):
            chain = row.arcs_ns or (0.0,)
            prev = f"i{i}"
            for j, value in enumerate(chain):
                net = f"o{i}" if j == len(chain) - 1 else f"n{i}_{j}"
                if net != f"o{i}":
                    v.write(f"wire {net};\n")
                v.write(f"{cells[value]} a{i}_{j} (.A({prev}), .Y({net}));\n")
                prev = net
            s.write(f"set_input_delay -clock epoch -max 0 [get_ports i{i}]\n")
            s.write(f"set_input_transition 0 [get_ports i{i}]\n")
            s.write(f"set_output_delay -clock epoch -max {epoch-(row.required_ns-row.launch_ns):.17g} [get_ports o{i}]\n")
        v.write("endmodule\n")
    (directory / "analyze.tcl").write_text(f'''proc analyze {{}} {{
  puts "global STA: read model"
  read_liberty global_timing.lib
  read_verilog global_timing.v
  link_design global_timing
  puts "global STA: read constraints"
  # These generated constraints use only exact internal port names. OpenSTA's
  # get_ports scans every port per call; bind through the indexed cell API.
  # Stream the same portable SDC, without writing another constraint copy.
  set ::emuflow_top [sta::top_instance]
  set ::emuflow_cell [$::emuflow_top cell]
  set constraints [open global_timing.sdc r]
  while {{[gets $constraints line] >= 0}} {{
    uplevel #0 [string map [list {{[get_ports }} {{[$::emuflow_cell find_port }}] $line]
  }}
  close $constraints
  set out [open measurements.tsv w]
  puts $out "endpoint\\tarrival_ns\\trequired_ns\\tslack_ns"
  puts "global STA: propagate timing"
  find_timing
  puts "global STA: query endpoint scalars"
  puts "global STA: serialize checks"
  for {{set i 0}} {{$i < {len(rows)}}} {{incr i}} {{
    set pin [$::emuflow_top find_pin o$i]
    if {{$pin == "NULL"}} {{ error "missing global STA endpoint o$i" }}
    set p "NULL"
    foreach vertex [$pin vertices] {{
      if {{$vertex != "NULL"}} {{
        set candidate [sta::vertex_worst_slack_path $vertex max]
        if {{$candidate != "NULL"}} {{ set p $candidate; break }}
      }}
    }}
    if {{$p == "NULL"}} {{ error "unconstrained global STA endpoint o$i" }}
    # Path scalar APIs use seconds. Querying the known endpoint directly is
    # linear and avoids materializing/sorting every PathEnd in one giant list.
    set arrival [expr {{[$p arrival] * 1.0e9}}]
    # Path.required/slack are vertex-tag values and omit the output-delay
    # PathEnd constraint. endpoint_slack includes that constraint and returns
    # user time units.  All generated checks belong to the epoch path group;
    # an empty group name matches no PathEnd in OpenSTA 3.1.
    set slack [sta::endpoint_slack $pin "epoch" max]
    set required [expr {{$arrival + $slack}}]
    puts $out "o$i\\t$arrival\\t$required\\t$slack"
  }}
  close $out
}}
if {{[catch {{analyze}} message]}} {{ puts stderr $message; exit 2 }}
exit 0
''')
    return rows


def read_measurements(
    path: Path,
    rows: Sequence[EventCheck],
    *,
    verify_arcs=True,
) -> _IndexedMeasurements:
    arrivals = array("d")
    required_values = array("d")
    slacks = array("d")
    with path.open() as stream:
        if next(stream, "").strip() != "endpoint\tarrival_ns\trequired_ns\tslack_ns":
            raise ValidationError("invalid global STA measurements header")
        for index, line in enumerate(stream):
            fields = line.rstrip("\n").split("\t")
            if (
                index >= len(rows)
                or len(fields) != 4
                or fields[0] != f"o{index}"
            ):
                raise ValidationError("duplicate/malformed global STA endpoint")
            try:
                numbers = tuple(float(v) for v in fields[1:])
            except ValueError as exc:
                raise ValidationError("invalid global STA measurement") from exc
            if not all(math.isfinite(x) for x in numbers):
                raise ValidationError("nonfinite global STA measurement")
            row = rows[index]
            if verify_arcs:
                # Check TX/commit rows as well as original-path observations.
                # This is an independent linear arc check, performed while the
                # measurement row is live instead of retaining a second map.
                arrival = math.fsum(row.arcs_ns)
                required = row.required_ns - row.launch_ns
                for actual, expected in zip(
                    numbers,
                    (arrival, required, required - arrival),
                ):
                    if abs(actual - expected) > max(
                        1e-3, 4 * 2**-23 * abs(expected)
                    ):
                        raise ValidationError(
                            f"global STA event binding disagrees at o{index}"
                        )
            arrivals.append(numbers[0] + row.launch_ns)
            required_values.append(numbers[1] + row.launch_ns)
            slacks.append(numbers[2])
    if len(arrivals) != len(rows):
        raise ValidationError("global STA missing/extra/unconstrained endpoints")
    return _IndexedMeasurements(rows, arrivals, required_values, slacks)


def _compress_path_equivalent_checks(
        rows: Sequence[EventCheck]) -> tuple[Sequence[EventCheck], Sequence[int]]:
    """Deduplicate complete, numerically identical original-path bundles.

    Every exported check is an independent timing chain.  Two original paths
    therefore have identical OpenSTA results exactly when their ordered
    target/runtime/transport check bundles have the same role, launch, arcs
    and deadline.  Event and path identities are deliberately excluded from
    the numerical signature, but the whole bundle is the unit of compression:
    individual TX, commit or observation rows are never merged in isolation.
    """
    if (
        isinstance(rows, _IndexedEventChecks)
        and rows._original_to_representative is not None
        and rows._representative_indices is not None
    ):
        if any(
            index == _UINT32_MAX
            or index >= len(rows._representative_indices)
            for index in rows._original_to_representative
        ):
            raise ValidationError("global STA path compression lost a timing check")
        return (
            _IndexedCheckSelection(rows, rows._representative_indices),
            rows._original_to_representative,
        )

    representative_indices = array("I")
    # A machine-word hash keeps one bounded key per distinct numerical bundle
    # instead of retaining every role/float/arc tuple a second time. Hash
    # matches are compared against the source rows, so hash collisions cannot
    # merge distinct path bundles.
    representative_bundles: dict[int, _RepresentativeBundle] = {}
    # This relation has one value per exported check. A Python ``list[int]``
    # costs at least one pointer per entry, while representative indices fit a
    # fixed-width unsigned value.  Reserve UINT32_MAX as the fail-closed
    # sentinel and keep the production relation at exactly four bytes/check.
    missing_representative = _UINT32_MAX
    if len(rows) >= missing_representative:
        raise ValidationError(
            "global STA check population exceeds indexed expansion"
        )
    original_to_representative = array(
        "I", (missing_representative for _ in rows)
    )

    def numerical_hash(index: int) -> int:
        if isinstance(rows, _IndexedEventChecks):
            return rows._numerical_hash(index)
        return _event_check_row_hash(rows[index])

    def numerical_key(index: int) -> tuple:
        if isinstance(rows, _IndexedEventChecks):
            return rows._numerical_key(index)
        row = rows[index]
        return row.role, row.launch_ns, row.arcs_ns, row.required_ns

    def bundle_hash(indices: Sequence[int]) -> int:
        value = len(indices)
        for index in indices:
            value = ((value * 1_000_003) ^ numerical_hash(index)) & _UINT64_MASK
        return value

    def add_bundle(indices: Sequence[int]) -> None:
        signature = bundle_hash(indices)
        start = None
        candidate_key = None
        candidate = representative_bundles.get(signature)
        while candidate is not None:
            if candidate.count != len(indices):
                candidate = candidate.collision
                continue
            if candidate.numerical_key is None:
                representative_key = tuple(
                    numerical_key(
                        representative_indices[candidate.start + offset]
                    )
                    for offset in range(candidate.count)
                )
                candidate.numerical_key = representative_key
            else:
                representative_key = candidate.numerical_key
            if candidate_key is None:
                candidate_key = tuple(numerical_key(index) for index in indices)
            if candidate_key == representative_key:
                start = candidate.start
                break
            candidate = candidate.collision
        if start is None:
            start = len(representative_indices)
            if start + len(indices) >= _UINT32_MAX:
                raise ValidationError(
                    "global STA representative population exceeds indexed storage"
                )
            representative_indices.extend(indices)
            representative_bundles[signature] = _RepresentativeBundle(
                start,
                len(indices),
                candidate_key,
                representative_bundles.get(signature),
            )
        for offset, index in enumerate(indices):
            original_to_representative[index] = start + offset

    # bind_physical_checks emits one contiguous bundle per original path. Keep
    # only the active bundle in that production order instead of a dictionary
    # containing one index list for every path. Preserve the public helper's
    # historical acceptance of interleaved callers through a compatibility
    # fallback.
    seen_paths = set()
    current_path = None
    current_indices: list[int] = []
    interleaved = False
    path_identities = (
        enumerate(rows._path_indices)
        if isinstance(rows, _IndexedEventChecks)
        else ((index, row.path) for index, row in enumerate(rows))
    )
    for index, path_identity in path_identities:
        if path_identity != current_path:
            if path_identity in seen_paths:
                interleaved = True
                break
            if current_indices:
                add_bundle(current_indices)
            current_path = path_identity
            seen_paths.add(path_identity)
            current_indices = []
        current_indices.append(index)
    if interleaved:
        representative_indices = array("I")
        representative_bundles.clear()
        original_to_representative = array(
            "I", (missing_representative for _ in rows)
        )
        path_indices: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            path_indices.setdefault(row.path, []).append(index)
        for indices in path_indices.values():
            add_bundle(indices)
    elif current_indices:
        add_bundle(current_indices)
    if any(
        index == missing_representative
        for index in original_to_representative
    ):
        raise ValidationError("global STA path compression lost a timing check")
    return _IndexedCheckSelection(rows, representative_indices), original_to_representative


class _ExpandedMeasurementSequence(Sequence):
    """Lazy identity overlay for expanded representative measurements.

    OpenSTA evaluates only representative path bundles.  The persisted TSV
    still contains every original check, but the in-process consumer only
    needs an indexed view which combines the original identity with the
    representative scalar row.  Keeping that overlay lazy avoids allocating a
    second check-sized population of Python dictionaries before stitching.
    """

    def __init__(self, rows: Sequence[EventCheck], representative_values: Sequence,
                 original_to_representative: Sequence[int]):
        if len(original_to_representative) != len(rows):
            raise ValidationError("global STA expansion population disagrees")
        self._rows = rows
        self._representative_values = representative_values
        self._original_to_representative = original_to_representative

    def __len__(self):
        return len(self._rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        representative_index = self._original_to_representative[index]
        if (
            isinstance(representative_index, bool)
            or not isinstance(representative_index, int)
            or representative_index < 0
            or representative_index >= len(self._representative_values)
        ):
            raise ValidationError(
                "global STA expansion references a missing representative"
            )
        row = self._rows[index]
        value = self._representative_values[representative_index]
        return {
            "path": row.path,
            "role": row.role,
            "event": row.event,
            "arrival_ns": value["arrival_ns"],
            "required_ns": value["required_ns"],
            "slack_ns": value["slack_ns"],
        }

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))


def _write_expanded_measurements(path: Path, rows: Sequence[EventCheck],
                                 representative_values: Sequence,
                                 original_to_representative: Sequence[int],
                                 representative_checks: Sequence[EventCheck]
                                 | None = None,
                                 ) -> Sequence[dict]:
    """Restore the full TSV and return a lazy original-identity overlay."""
    if (
        representative_checks is not None
        and len(representative_checks) != len(representative_values)
    ):
        raise ValidationError("global STA representative population disagrees")
    result = _ExpandedMeasurementSequence(
        rows, representative_values, original_to_representative
    )
    temporary = path.with_name(f".{path.name}.expanded.tmp")
    try:
        with temporary.open("w") as stream:
            stream.write("endpoint\tarrival_ns\trequired_ns\tslack_ns\n")
            for index, row in enumerate(rows):
                representative_index = original_to_representative[index]
                if representative_checks is not None:
                    if representative_index >= len(representative_checks):
                        raise ValidationError(
                            "global STA expansion references a missing representative"
                        )
                    representative = representative_checks[representative_index]
                    if (
                        row.role,
                        row.launch_ns,
                        row.arcs_ns,
                        row.required_ns,
                    ) != (
                        representative.role,
                        representative.launch_ns,
                        representative.arcs_ns,
                        representative.required_ns,
                    ):
                        raise ValidationError(
                            "global STA expansion mapping disagrees"
                        )
                value = result[index]
                local_arrival = value["arrival_ns"] - row.launch_ns
                local_required = value["required_ns"] - row.launch_ns
                stream.write(
                    f"o{index}\t{local_arrival:.17g}\t{local_required:.17g}\t"
                    f"{value['slack_ns']:.17g}\n"
                )
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return result


def run_event_checks(checks: Iterable[EventCheck], directory: Path,
                     executable: str | None = None, *, verify_arcs=True
                     ) -> Sequence[dict]:
    rows = validate_checks(checks)
    representatives, original_to_representative = (
        _compress_path_equivalent_checks(rows)
    )
    export_event_checks(representatives, directory)
    tool = resolve_native_executable("sta", executable)
    engine = require_opensta_engine(tool)
    output = directory / "measurements.tsv"
    output.unlink(missing_ok=True)
    with (directory / "opensta.log").open("w") as log:
        # Some packaged OpenSTA builds report ``GITDIR-NOT`` because their
        # source archive had no .git directory. Seal the exact executable in
        # the process log so engine identity remains independently replayable.
        log.write(
            "EmuFlow OpenSTA executable SHA256 "
            f"{engine['executable_sha256']}\n"
        )
        log.flush()
        result = subprocess.run([tool, "-exit", "analyze.tcl"], cwd=directory,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode or not output.is_file():
        raise ValidationError("global OpenSTA failed; see opensta.log")
    representative_values = read_measurements(
        output, representatives, verify_arcs=verify_arcs
    )
    expanded_values = _write_expanded_measurements(
        output,
        rows,
        representative_values,
        original_to_representative,
        representatives,
    )
    with (directory / "opensta.log").open("a") as log:
        log.write(
            "EmuFlow exact path-bundle compression "
            f"original_checks={len(rows)} "
            f"representative_checks={len(representatives)}\n"
        )
    return expanded_values


def read_engine_identity(log_path: Path) -> dict:
    """Read the existing process banner, without another tool invocation."""
    executable_sha256 = None
    banner = None
    with log_path.open() as stream:
        for _ in range(32):
            line = stream.readline(4096)
            if not line:
                break
            seal = re.fullmatch(
                r"EmuFlow OpenSTA executable SHA256 ([0-9a-f]{64})\n?", line
            )
            if seal:
                executable_sha256 = seal[1]
                continue
            match = re.match(r"OpenSTA\s+(\S+)\s+(\S+)", line)
            if match:
                banner = (match[1], match[2])
    if banner is not None:
        version, reported_revision = banner
        if re.fullmatch(r"[0-9a-f]{7,40}", reported_revision):
            result = {
                "name": "OpenSTA",
                "version": version,
                "revision": reported_revision,
            }
            if executable_sha256 is not None:
                result["executable_sha256"] = executable_sha256
            return result
        if executable_sha256 is not None:
            return {
                "name": "OpenSTA",
                "version": version,
                "revision": f"binary-sha256:{executable_sha256}",
                "reported_revision": reported_revision,
                "executable_sha256": executable_sha256,
            }
    raise ValidationError("global OpenSTA log lacks engine version/revision")


def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    """Bind canonical measurements to event checks without composing delays.

Shared database readers validate identities only. This exporter does not call
the Python event propagator or consume its per-path numerical results.
"""
    from .system_timing import (
        _board_link_delay_database, _endpoint_delay_database,
        _logic_segment_database,
    )
    from .tdm import (
        is_fixed_slot_transport_schedule,
        open_tdm_schedule_timing_path_stream,
    )

    if not is_fixed_slot_transport_schedule(schedule):
        raise ValidationError("global OpenSTA requires fixed-slot transport")
    endpoints = _endpoint_delay_database(physical)
    segments = _logic_segment_database(physical)
    links = _board_link_delay_database(physical, platform)
    if links is None:
        # Preserve the ordinary academic flow's declared BoardDB cycle latency,
        # explicitly model-only, never guessed or supposedly measured timing.
        from .board_link_timing import build_board_link_timing_model
        links = _board_link_delay_database(
            {"board_link_timing": build_board_link_timing_model(platform)}, platform)
    if endpoints is None or segments is None or links is None:
        raise ValidationError("global OpenSTA requires physical logic and endpoint measurements")
    # Timing reconstruction already binds each routed hop to its schedule
    # entry and carries the concrete TX time/ratio in ``scheduled_hops``.
    # Keep a compatibility lookup only for older injected reconstruction
    # records; production must not rebuild a second hop-sized entry map.
    legacy_entries = None

    def legacy_entry(identity):
        nonlocal legacy_entries
        if legacy_entries is None:
            legacy_entries = {e["id"]: e for e in schedule["entries"]}
        try:
            return legacy_entries[identity]
        except KeyError as exc:
            raise ValidationError(
                "global STA references a missing scheduled event"
            ) from exc
    board_links = {link.id: link for link in platform.links}
    uncertainty = float(physical.get("static_exact_clock_uncertainty_ns", 0.0))
    if uncertainty < 0 or not math.isfinite(uncertainty):
        raise ValidationError("invalid global STA uncertainty")
    from .runtime import runtime_clock_period_ns
    result = _EventCheckBuilder()
    path_metadata = _PathMetadataBuilder()
    representatives = 0
    segment_cursor = (
        segments.ordered_cursor()
        if callable(getattr(segments, "ordered_cursor", None))
        else None
    )
    try:
        with open_tdm_schedule_timing_path_stream(
            routes, platform, schedule, path_order="path"
        ) as reconstructed:
            for record in reconstructed:
                virtual = runtime_clock_period_ns(
                    runtime, record.get("clock_domain")
                )
                representatives += 1
                by_demand = {}
                for hop in record["scheduled_hops"]:
                    by_demand.setdefault(hop["demand"], []).append(hop)
                cut_demands = record.get("_cut_demands")
                if (
                    not isinstance(cut_demands, list)
                    or len(cut_demands) != len(record["cut_nets"])
                ):
                    raise ValidationError(
                        "global STA timing stream lacks cut-demand binding"
                    )
                members = sorted(
                    record.get("compressed_path_ids", [record["path"]])
                )
                for member in members:
                    chain = list(segment_cursor.take(record["path"], member)) if (
                        segment_cursor is not None
                    ) else sorted(
                        segments.get(record["path"], {}).get(member, []),
                        key=lambda s: s["cut_index"],
                    )
                    if [s["cut_index"] for s in chain] != list(range(len(record["cut_nets"])+1)):
                        raise ValidationError("global STA missing original-member logic segments")
                    origin = 0.0
                    trailing = ()
                    ratio_mode = any(board_links[h["link"]].tdm_delay_model is not None
                                     for h in record["scheduled_hops"])
                    if ratio_mode and not all(board_links[h["link"]].tdm_delay_model is not None
                                              for h in record["scheduled_hops"]):
                        raise ValidationError("one path cannot mix total net-ratio and fixed-slot delay models")
                    model_arcs = []
                    path_metadata.append(
                        member,
                        representative_path=record["path"],
                        path_scope="cross-fpga",
                        clock_domain=record.get("clock_domain"),
                        target_period_ns=record["clock_period_ns"],
                        physical_logic_segments_cone_bound=any(
                            s.get("measurement") == "cut-net-cone-upper-bound"
                            for s in chain
                        ),
                        interconnect_delay_model=(
                            "linear-ratio-v1" if ratio_mode else None
                        ),
                    )
                    periods = set()
                    seen_entries = set()
                    for k, (net, demand) in enumerate(
                        zip(record["cut_nets"], cut_demands)
                    ):
                        hops = by_demand.get(demand, [])
                        if not hops or chain[k]["replace_tx_endpoint"] != hops[0]["tx_endpoint"]:
                            raise ValidationError("global STA cut/segment binding disagrees")
                        for j, hop in enumerate(hops):
                            eid = hop["schedule_entry"]
                            if eid in seen_entries:
                                raise ValidationError("global STA reused a scheduled event")
                            seen_entries.add(eid)
                            period = float(hop["tdm_slot_ns"])
                            periods.add(period)
                            if "fixed_slot_tx_time_ns" in hop:
                                tx = float(hop["fixed_slot_tx_time_ns"])
                                ratio = hop.get("tdm_ratio")
                            else:
                                entry = legacy_entry(eid)
                                for field in ("link", "from", "to"):
                                    if entry[field] != hop[field]:
                                        raise ValidationError(
                                            "global STA hop/event identity disagrees"
                                        )
                                tx = entry["slot"] * period
                                ratio = entry.get("tdm_ratio")
                            if not math.isfinite(tx) or tx < 0.0:
                                raise ValidationError("global STA TX time is invalid")
                            arcs = trailing + (float(chain[k]["delay_ns"]) if not j else
                                               endpoints[hop["tx_endpoint"]],)
                            delay_model = board_links[hop["link"]].tdm_delay_model
                            link_delay = (delay_model.delay_ns(ratio)
                                          if delay_model is not None else
                                          links[hop["link"], hop["from"], hop["to"]])
                            if ratio_mode:
                                model_arcs.extend((arcs[-1], link_delay, endpoints[hop["rx_endpoint"]]))
                            # Previous-frame registered values are stable at first TX.
                            # Only that check is exempt; relays retain current-frame causality.
                            if not (schedule["transport_semantics"] == "registered-boundary" and not j):
                                result.append_values(
                                    member, "tx", eid, origin, arcs, tx - uncertainty
                                )
                            origin = tx
                            trailing = (link_delay,
                                        endpoints[hop["rx_endpoint"]])
                    if len(periods) != 1 or len(seen_entries) != len(record["scheduled_hops"]):
                        raise ValidationError("global STA incomplete event coverage or mixed slot periods")
                    if chain[-1]["kind"] != "capture" or chain[-1]["replace_tx_endpoint"] is not None:
                        raise ValidationError("global STA final segment is not capture")
                    final_arcs = trailing + (float(chain[-1]["delay_ns"]), uncertainty)
                    required = float(record.get("required_time_ns", record["clock_period_ns"]))
                    commit = (schedule["route_constraints"]["frame_slots"]-1)*periods.pop()
                    for role, deadline in (("target", required),
                                           ("runtime", virtual-(record["clock_period_ns"]-required)),
                                           ("commit", commit)):
                        # The total net-ratio model already includes TDM delay.
                        # Keep absolute TX/commit readiness checks separate; never
                        # add their slot wait again to the modeled path delay.
                        if ratio_mode and role in {"target", "runtime"}:
                            result.append_values(
                                member,
                                role,
                                "capture",
                                0.0,
                                tuple(model_arcs)
                                + (float(chain[-1]["delay_ns"]), uncertainty),
                                deadline,
                            )
                        else:
                            result.append_values(
                                member, role, "capture", origin, final_arcs, deadline
                            )
        if segment_cursor is not None:
            segment_cursor.finish()
    finally:
        if segment_cursor is not None:
            segment_cursor.close()
    source = None
    from .local_path_timing import validate_local_path_timing, path_id_set_sha256
    for fpga, database in physical.get("local_path_timing", {}).items():
        if metadata is not None:
            if validate_local_path_timing(database)["fpga"] != fpga:
                raise ValidationError("global STA local FPGA identity disagrees")
            if source is not None and source != database["source"]:
                raise ValidationError("global STA local source seals disagree")
            source = database["source"]
        for path in database["paths"]:
            path_metadata.append(
                path["id"],
                representative_path=path["id"],
                path_scope="same-fpga-local",
                clock_domain=path.get("clock_domain"),
                target_period_ns=path["clock_period_ns"],
                physical_logic_segments_cone_bound=(
                    path.get("measurement") != "explicit-routed-path-chain"
                ),
            )
            required = float(path.get("required_time_ns", path["clock_period_ns"]))
            virtual = runtime_clock_period_ns(
                runtime, path.get("clock_domain")
            )
            for role, deadline in (("target", required),
                                   ("runtime", virtual-(path["clock_period_ns"]-required))):
                result.append_values(
                    path["id"],
                    role,
                    "capture",
                    0.0,
                    (float(path["delay_ns"]),),
                    deadline,
                )
    rows = validate_checks(result.finish())
    indexed_path_metadata = path_metadata.finish()
    if metadata is not None:
        if source is not None and (
            source["routes_sha256"] != routes_artifact_sha256
            or source["original_paths"] != len(indexed_path_metadata)
            or source["original_path_ids_sha256"]
            != indexed_path_metadata.path_id_set_sha256()
        ):
            raise ValidationError("global STA original population/source binding disagrees")
        metadata.update(paths=indexed_path_metadata, source_binding=source,
                        compressed_representative_paths=representatives)
    return rows


def build_opensta_timing(runtime, metadata, measurements):
    """Build the canonical report directly from engine scalars, never Python timing.

    Binding owns coverage and physical qualifications. OpenSTA owns numerical
    propagation. Python only aggregates the project's original-path metric.
    """
    metadata_paths = metadata["paths"]

    class InterleavedMeasurements(Exception):
        pass

    def ordered_paths():
        # bind_physical_checks emits one contiguous bundle per original path.
        # Validate identities within the live bundle and construct the final
        # path row when that bundle ends. This avoids an H-sized identity set
        # and a second P-sized target/runtime record map in production.
        typed_builder = (
            _SystemTimingPathBuilder(metadata_paths)
            if isinstance(metadata_paths, _IndexedPathMetadata)
            else None
        )
        paths = [] if typed_builder is None else None
        completed = set() if typed_builder is None else None
        current_path = None
        current_rows = {}
        current_identities = set()
        failures = 0
        checks = 0

        def finish_bundle():
            if current_path is None:
                return
            if set(current_rows) != {"target", "runtime"}:
                raise InterleavedMeasurements
            info = metadata_paths[current_path]
            target = current_rows["target"]
            runtime_row = current_rows["runtime"]
            if typed_builder is not None:
                if not typed_builder.append(current_path, target, runtime_row):
                    raise InterleavedMeasurements
            else:
                assert paths is not None and completed is not None
                path = {
                    "path": current_path,
                    **info,
                    "system_delay_bound_ns": target["arrival_ns"],
                    "target_required_time_ns": target["required_ns"],
                    "target_clock_slack_bound_ns": target["slack_ns"],
                    "runtime_required_time_ns": runtime_row["required_ns"],
                    "runtime_clock_slack_bound_ns": runtime_row["slack_ns"],
                }
                paths.append(path)
                completed.add(current_path)

        for row in measurements:
            checks += 1
            path_id = row.get("path")
            role = row.get("role")
            event = row.get("event")
            if (
                path_id not in metadata_paths
                or role not in {"target", "runtime", "tx", "commit"}
                or not isinstance(event, str)
                or not event
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            if path_id != current_path:
                if completed is not None and path_id in completed:
                    raise InterleavedMeasurements
                finish_bundle()
                current_path = path_id
                current_rows = {}
                current_identities = set()
            identity = (role, event)
            if identity in current_identities:
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            current_identities.add(identity)
            if any(
                isinstance(row.get(key), bool)
                or not isinstance(row.get(key), (int, float))
                or not math.isfinite(row[key])
                for key in ("arrival_ns", "required_ns", "slack_ns")
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            if role in {"target", "runtime"}:
                if role in current_rows:
                    raise ValidationError(
                        "global STA invalid standalone measurement population"
                    )
                current_rows[role] = row
            elif row["slack_ns"] < -1e-3:
                failures += 1
        finish_bundle()
        if typed_builder is not None:
            paths = typed_builder.finish()
        else:
            assert paths is not None
            if not paths or len(paths) != len(metadata_paths):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            paths.sort(key=lambda path: path["path"])
        return paths, failures, checks

    try:
        paths, failures, measurement_count = ordered_paths()
    except InterleavedMeasurements:
        # Preserve compatibility for external callers that do not retain the
        # canonical path-bundle order. This is outside the production path.
        observed = {}
        identities = set()
        failures = 0
        for row in measurements:
            identity = (row.get("path"), row.get("role"), row.get("event"))
            if (
                row.get("path") not in metadata_paths
                or row.get("role") not in {"target", "runtime", "tx", "commit"}
                or identity in identities
                or any(
                    isinstance(row.get(key), bool)
                    or not isinstance(row.get(key), (int, float))
                    or not math.isfinite(row[key])
                    for key in ("arrival_ns", "required_ns", "slack_ns")
                )
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            identities.add(identity)
            if row["role"] in {"target", "runtime"}:
                key = (row["path"], row["role"])
                if key in observed:
                    raise ValidationError(
                        "global STA invalid standalone measurement population"
                    )
                observed[key] = row
            elif row["slack_ns"] < -1e-3:
                failures += 1
        expected = {
            (path_id, role)
            for path_id in metadata_paths
            for role in ("target", "runtime")
        }
        if set(observed) != expected:
            raise ValidationError(
                "global STA invalid standalone measurement population"
            )
        paths = []
        for path_id, info in sorted(metadata_paths.items()):
            target = observed[path_id, "target"]
            path = {
                "path": path_id,
                **info,
                "system_delay_bound_ns": target["arrival_ns"],
            }
            for role in ("target", "runtime"):
                row = observed[path_id, role]
                path[f"{role}_required_time_ns"] = row["required_ns"]
                path[f"{role}_clock_slack_bound_ns"] = row["slack_ns"]
            paths.append(path)
        measurement_count = len(measurements)
    if not paths:
        raise ValidationError("global STA empty standalone result")
    source = metadata["source_binding"]
    scope = "whole-original-design" if source is not None else "cross-fpga-path-subset"
    local = 0
    bounds = 0
    maximum_delay = -math.inf
    metrics = {
        role: {
            "wns_ns": math.inf,
            "original_path_tns_ns": 0.0,
            "negative_paths": 0,
        }
        for role in ("target", "runtime")
    }
    worst_paths = {role: None for role in ("target", "runtime")}
    negative_partials = {role: [] for role in ("target", "runtime")}

    def add_fsum(partials: list[float], value: float) -> None:
        """Accumulate one value with math.fsum's bounded partials strategy."""

        index = 0
        for partial in partials:
            if abs(value) < abs(partial):
                value, partial = partial, value
            high = value + partial
            low = partial - (high - value)
            if low:
                partials[index] = low
                index += 1
            value = high
        partials[index:] = [value]

    for path in paths:
        local += path["path_scope"] == "same-fpga-local"
        bounds += path["physical_logic_segments_cone_bound"]
        maximum_delay = max(maximum_delay, path["system_delay_bound_ns"])
        for role in ("target", "runtime"):
            slack = path[f"{role}_clock_slack_bound_ns"]
            metric = metrics[role]
            if (slack, path["path"]) < (
                metric["wns_ns"], worst_paths[role] or ""
            ):
                metric["wns_ns"] = slack
                worst_paths[role] = path["path"]
            if slack < 0.0:
                metric["negative_paths"] += 1
                add_fsum(negative_partials[role], slack)
    for role in ("target", "runtime"):
        metrics[role]["original_path_tns_ns"] = math.fsum(
            negative_partials[role]
        )
    timing = {"schema": "emuflow.system-timing/v2", "status": "fail" if failures else "pass",
              "qualification": "opensta-fixed-event-physical-bounds", "timing_scope": scope,
              "source_binding": source, "paths": paths,
              "path_exactness": {"physical_model": "routed-staging-chain-upper-bounds",
                  "endpoint_exact_logic_paths": len(paths)-bounds, "cone_bound_logic_paths": bounds,
                  "fallback_logic_paths": 0, "discontinuous_compressed_paths": 0,
                  "physical_logic_segment_bounds": True, "fixed_slot_event_failures": failures},
              "summary": {"timing_paths": len(paths), "original_paths": source["original_paths"] if source else None,
                  "original_local_paths": local, "original_cross_fpga_paths": len(paths)-local,
                  "compressed_representative_paths": metadata["compressed_representative_paths"],
                  "original_path_coverage": 1.0 if source else None,
                  "original_path_ids_sha256": source["original_path_ids_sha256"] if source else None,
                  "maximum_system_delay_bound_ns": maximum_delay}}
    for role in ("target", "runtime"):
        metric = metrics[role]
        tns = metric["original_path_tns_ns"]
        timing[f"{role}_clock"] = {"closure_gate": role=="runtime", "worst_path": worst_paths[role],
            "worst_slack_bound_ns": metric["wns_ns"], "tns_bound_ns": tns,
            "total_negative_slack_bound_ns": tns, "negative_slack_paths": metric["negative_paths"]}
    clock_periods = {
        clock: float(record["nominal_period_ns"])
        for clock, record in runtime["virtual_dut_clocks"].items()
    }
    period = min(clock_periods.values())
    timing["runtime_clock"].update(
        period_ns=period,
        frequency_mhz=1000.0 / period,
        period_scope="fastest-domain-summary",
        clock_periods_ns=clock_periods,
    )
    maximum = timing["summary"]["maximum_system_delay_bound_ns"]
    timing["runtime_clock"].update(minimum_safe_period_bound_ns=maximum,
        maximum_safe_frequency_bound_mhz=1000.0/maximum if maximum > 0 else None)
    if metrics["runtime"]["wns_ns"] < 0:
        timing["status"] = "fail"
    timing["global_opensta"] = {"schema": "emuflow.global-opensta-check/v1",
        "authority": "opensta", "execution": "standalone", "status": timing["status"],
        "timing_scope": scope, "original_paths": len(paths), "checks": measurement_count,
        "event_failures": failures, "metrics": metrics,
        "tns_definition": "sum-negative-slack-once-per-original-TimingPathDB-path"}
    return timing


def compare_system_timing(measurements, reference, *, tolerance_ns=1.0e-3):
    """Compare every original path, not merely extrema; return a compact gate.

    OpenSTA uses float-based timing internally. The floor is 1 ps, with four
    float32 relative epsilons on the specific compared value (not the global
    frame). A near-zero slack retains the strict floor even in a huge frame.
"""
    if tolerance_ns <= 0 or not math.isfinite(tolerance_ns):
        raise ValidationError("invalid global STA comparison tolerance")
    expected = {p["path"]: p for p in reference["paths"]}
    if not expected or len(expected) != len(reference["paths"]):
        raise ValidationError("global STA reference has empty/duplicate original paths")
    observed = {}
    identities = set()
    max_error = 0.0
    max_tolerance = tolerance_ns
    failures = 0
    for row in measurements:
        role = row["role"]
        identity = (row["path"], role, row["event"])
        if (role not in {"target", "runtime", "tx", "commit"}
                or row["path"] not in expected or identity in identities):
            raise ValidationError("global STA invalid/duplicate measurement identity")
        identities.add(identity)
        if any(isinstance(row[k], bool) or not math.isfinite(row[k])
               for k in ("arrival_ns", "required_ns", "slack_ns")):
            raise ValidationError("global STA nonfinite measurement")
        if role in {"tx", "commit"}:
            failures += row["slack_ns"] < -tolerance_ns
            continue
        key = (row["path"], role)
        if key in observed or row["path"] not in expected:
            raise ValidationError("global OpenSTA original-path population mismatch")
        observed[key] = row
        old = expected[row["path"]]
        for actual, value in ((row["arrival_ns"], old["system_delay_bound_ns"]),
                              (row["required_ns"], old[f"{role}_required_time_ns"]),
                              (row["slack_ns"], old[f"{role}_clock_slack_bound_ns"])):
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValidationError("global STA nonfinite reference")
            error = abs(actual-value)
            max_error = max(max_error, error)
            allowance = max(tolerance_ns, 4 * 2**-23 * abs(value))
            max_tolerance = max(max_tolerance, allowance)
            if error > allowance:
                raise ValidationError(f"global OpenSTA disagrees at {key}: error {error} ns")
    if set(observed) != {(p, r) for p in expected for r in ("target", "runtime")}:
        raise ValidationError("global OpenSTA original-path coverage is incomplete")
    summary = {}
    for role in ("target", "runtime"):
        slacks = [row["slack_ns"] for row in measurements if row["role"] == role]
        summary[role] = {"wns_ns": min(slacks),
                         "original_path_tns_ns": math.fsum(min(0.0, s) for s in slacks),
                         "negative_paths": sum(s < 0 for s in slacks)}
    return {"schema": "emuflow.global-opensta-check/v1", "status": "fail" if failures else "pass",
            "authority": "qualification-only-pending-full-flow-validation",
            "timing_scope": reference["timing_scope"], "original_paths": len(expected),
            "event_failures": failures, "checks": len(measurements),
            "tolerance_ns": tolerance_ns, "maximum_difference_ns": max_error,
            "maximum_comparison_tolerance_ns": max_tolerance,
            "relative_float32_epsilons": 4,
            "metrics": summary,
            "tns_definition": "sum-negative-slack-once-per-original-TimingPathDB-path"}


def adopt_opensta_results(timing, measurements):
    """Project checked engine values into the canonical report, in place.

    Keep the independent composer as the comparison input, not a second persisted path
    population. All public scalar aliases are updated together.
    """
    gate = compare_system_timing(measurements, timing)
    observed = {(r["path"], r["role"]): r for r in measurements
                if r["role"] in {"target", "runtime"}}
    for path in timing["paths"]:
        path["system_delay_bound_ns"] = observed[path["path"], "target"]["arrival_ns"]
        for role in ("target", "runtime"):
            row = observed[path["path"], role]
            path[f"{role}_required_time_ns"] = row["required_ns"]
            path[f"{role}_clock_slack_bound_ns"] = row["slack_ns"]
    for role in ("target", "runtime"):
        worst = min(timing["paths"], key=lambda p: (
            p[f"{role}_clock_slack_bound_ns"], p["path"]))
        metrics = gate["metrics"][role]
        timing[f"{role}_clock"].update({
            "worst_path": worst["path"],
            "worst_slack_bound_ns": metrics["wns_ns"],
            "negative_slack_paths": metrics["negative_paths"],
            "total_negative_slack_bound_ns": metrics["original_path_tns_ns"],
            "tns_bound_ns": metrics["original_path_tns_ns"],
        })
    maximum = max(p["system_delay_bound_ns"] for p in timing["paths"])
    timing["summary"]["maximum_system_delay_bound_ns"] = maximum
    timing["runtime_clock"]["minimum_safe_period_bound_ns"] = maximum
    timing["runtime_clock"]["maximum_safe_frequency_bound_mhz"] = (
        1000.0 / maximum if maximum > 0 else None)
    if gate["status"] != "pass" or gate["metrics"]["runtime"]["wns_ns"] < 0:
        timing["status"] = "fail"
    gate["authority"] = "opensta"
    gate["cross_checker"] = "independent-python-event-composer"
    timing["global_opensta"] = gate
    return gate
