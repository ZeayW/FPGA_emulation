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
import os
import re
import sqlite3
import subprocess
import tempfile
from array import array
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .native_tools import resolve_native_executable
from .opensta import render_opensta_liberty, require_opensta_engine
from .timing_disposition import (OPTIMIZED_AWAY, PATH_POPULATION_POLICY,
                                 is_optimized_away, validate_optimization_record,
                                 require_optimization_proof)


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


class _PackedUtf8Strings(Sequence[str]):
    """One UTF-8 payload plus fixed-width offsets for a string identity set."""

    def __init__(self, payload: bytearray, offsets: array):
        if (
            offsets.typecode != "Q"
            or not offsets
            or offsets[0] != 0
            or offsets[-1] != len(payload)
            or any(
                offsets[index - 1] > offsets[index]
                for index in range(1, len(offsets))
            )
        ):
            raise ValidationError("global STA packed string pool is malformed")
        self._payload = payload
        self._offsets = offsets

    def __len__(self) -> int:
        return len(self._offsets) - 1

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self._payload[
            self._offsets[index] : self._offsets[index + 1]
        ].decode("utf-8")

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    @property
    def packed_bytes(self) -> int:
        return len(self._payload) + len(self._offsets) * self._offsets.itemsize


class _PackedStringSelection(Sequence[str]):
    """A fixed-width ordered projection over a shared packed string pool."""

    def __init__(self, strings: _PackedUtf8Strings, indices: array):
        if indices.typecode != "I" or any(index >= len(strings) for index in indices):
            raise ValidationError("global STA packed string selection is malformed")
        self._strings = strings
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
        return self._strings[self._indices[index]]

    def __iter__(self):
        for index in self._indices:
            yield self._strings[index]


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
        strings: _PackedUtf8Strings,
        path_indices: array,
        representative_indices: array,
        scopes: array,
        clock_indices: array,
        target_periods: array,
        cone_bounds: array,
        model_indices: array,
        optimization_sources: array | None = None,
        optimization_fpgas: array | None = None,
        *,
        validated: bool = False,
    ):
        paths = _PackedStringSelection(strings, path_indices)
        count = len(path_indices)
        if (
            len(representative_indices) != count
            or len(scopes) != count
            or len(clock_indices) != count
            or len(target_periods) != count
            or len(cone_bounds) != count
            or len(model_indices) != count
            or (
                not validated
                and (
                    any(not path for path in paths)
                    or any(
                        paths[index - 1] >= paths[index]
                        for index in range(1, count)
                    )
                )
            )
        ):
            raise ValidationError("global STA indexed path metadata is malformed")
        self._paths = paths
        self._strings = strings
        self._path_indices = path_indices
        self._representative_indices = representative_indices
        self._scopes = scopes
        self._clock_indices = clock_indices
        self._target_periods = target_periods
        self._cone_bounds = cone_bounds
        self._model_indices = model_indices
        if (optimization_sources is None) != (optimization_fpgas is None) or (
            optimization_sources is not None and (len(optimization_sources) != count or len(optimization_fpgas) != count)
        ):
            raise ValidationError("global STA optimization metadata columns disagree")
        self._optimization_sources = optimization_sources
        self._optimization_fpgas = optimization_fpgas

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
        result = {
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
        if self._optimization_sources is not None:
            source = self._optimization_sources[index]
            fpga = self._optimization_fpgas[index]
            if (source == _UINT32_MAX) != (fpga == _UINT32_MAX):
                raise ValidationError("global STA optimization metadata reference disagrees")
            if source != _UINT32_MAX:
                if source >= len(self._strings) or fpga >= len(self._strings):
                    raise ValidationError("global STA optimization metadata string is invalid")
                result.update(measurement=OPTIMIZED_AWAY, optimization_source=self._strings[source],
                              optimization_fpga=self._strings[fpga])
        return result

    @property
    def typed_payload_bytes(self) -> int:
        """Bytes in fixed-width columns, excluding the shared UTF-8 pool."""

        return sum(
            values.itemsize * len(values)
            for values in (
                self._path_indices,
                self._representative_indices,
                self._scopes,
                self._clock_indices,
                self._target_periods,
                self._cone_bounds,
                self._model_indices,
                self._optimization_sources,
                self._optimization_fpgas,
            )
            if values is not None
        )

    @property
    def packed_identity_bytes(self) -> int:
        """Bytes retained for all unique identities and their offsets."""

        return self._strings.packed_bytes

    def path_id_set_sha256(self) -> str:
        """Hash the already sorted unique path IDs without another copy/sort."""

        digest = hashlib.sha256()
        digest.update(b"[")
        for index, path in enumerate(self._paths):
            if index:
                digest.update(b",")
            digest.update(
                json.dumps(
                    path, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        digest.update(b"]")
        return digest.hexdigest()


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
        info = self._metadata[path]
        if is_optimized_away(info):
            validate_optimization_record(info)
            return {"path": path, **info, **dict.fromkeys(_RESULT_SCALARS)}
        return {
            "path": path,
            **info,
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
        self._seen = array("B", (int(is_optimized_away(metadata[p])) for p in metadata))
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
    """Spool path metadata, then seal sorted packed identity columns.

    Global STA can cover millions of original paths. Keeping path strings, an
    interning dictionary, a second string list, and the final sort order in
    Python made all of those populations overlap at ``finish()``. This owner
    writes bounded batches to a private typed SQLite relation. SQLite owns the
    external sort and duplicate detection; the sealed view retains one shared
    UTF-8 pool plus fixed-width columns only.
    """

    _BATCH_ROWS = 4096

    def __init__(self):
        self._finished = False
        descriptor, name = tempfile.mkstemp(
            prefix="emuflow-global-sta-paths-", suffix=".sqlite3"
        )
        os.close(descriptor)
        self._database_path = Path(name)
        self._connection: sqlite3.Connection | None = sqlite3.connect(name)
        self._connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-8192;
            CREATE TABLE path_metadata (
                path TEXT NOT NULL,
                representative_path TEXT NOT NULL,
                path_scope INTEGER NOT NULL CHECK(path_scope IN (0, 1)),
                clock_domain TEXT,
                target_period_ns REAL NOT NULL,
                cone_bound INTEGER NOT NULL CHECK(cone_bound IN (0, 1)),
                interconnect_delay_model TEXT,
                optimization_source TEXT,
                optimization_fpga TEXT
            );
            """
        )
        self._pending: list[tuple[object, ...]] = []

    def _flush(self) -> None:
        if not self._pending:
            return
        assert self._connection is not None
        self._connection.executemany(
            "INSERT INTO path_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._pending,
        )
        self._pending.clear()

    def _close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()
        self._database_path.unlink(missing_ok=True)

    def __del__(self):
        try:
            self._close()
        except BaseException:
            pass

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
        optimization_source: str | None = None,
        optimization_fpga: str | None = None,
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
        for value in (clock_domain, interconnect_delay_model):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValidationError("global STA path metadata string is invalid")
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
        if (optimization_source is None) != (optimization_fpga is None):
            raise ValidationError("global STA optimization metadata needs source and FPGA together")
        if optimization_fpga is not None and (not isinstance(optimization_fpga, str) or not optimization_fpga):
            raise ValidationError("global STA optimization FPGA identity is invalid")
        if optimization_source is not None:
            validate_optimization_record({"measurement": OPTIMIZED_AWAY,
                                          "optimization_source": optimization_source, "delay_ns": None})
        self._pending.append(
            (
                path,
                representative_path,
                path_scope == "same-fpga-local",
                clock_domain,
                float(target_period_ns),
                physical_logic_segments_cone_bound,
                interconnect_delay_model,
                optimization_source,
                optimization_fpga,
            )
        )
        if len(self._pending) >= self._BATCH_ROWS:
            self._flush()

    def finish(self) -> _IndexedPathMetadata:
        if self._finished:
            raise ValidationError("global STA path metadata builder is sealed")
        self._finished = True
        try:
            self._flush()
            assert self._connection is not None
            connection = self._connection
            connection.executescript(
                """
                CREATE TABLE string_identity (
                    identity INTEGER PRIMARY KEY,
                    value TEXT NOT NULL UNIQUE
                );
                INSERT INTO string_identity(value)
                    SELECT value FROM (
                        SELECT path AS value FROM path_metadata
                        UNION
                        SELECT representative_path AS value FROM path_metadata
                        UNION
                        SELECT clock_domain AS value FROM path_metadata
                            WHERE clock_domain IS NOT NULL
                        UNION
                        SELECT interconnect_delay_model AS value
                            FROM path_metadata
                            WHERE interconnect_delay_model IS NOT NULL
                        UNION
                        SELECT optimization_source FROM path_metadata WHERE optimization_source IS NOT NULL
                        UNION
                        SELECT optimization_fpga FROM path_metadata WHERE optimization_fpga IS NOT NULL
                    ) ORDER BY value;
                """
            )
            (identity_count,) = connection.execute(
                "SELECT COUNT(*) FROM string_identity"
            ).fetchone()
            if identity_count >= _UINT32_MAX:
                raise ValidationError(
                    "global STA path metadata string population is too large"
                )

            string_payload = bytearray()
            string_offsets = array("Q", [0])
            for (value,) in connection.execute(
                "SELECT value FROM string_identity ORDER BY identity"
            ):
                string_payload.extend(value.encode("utf-8"))
                string_offsets.append(len(string_payload))
            strings = _PackedUtf8Strings(string_payload, string_offsets)

            path_indices = array("I")
            representative_indices = array("I")
            scopes = array("B")
            clock_indices = array("I")
            target_periods = array("d")
            cone_bounds = array("B")
            model_indices = array("I")
            optimization_sources = array("I")
            optimization_fpgas = array("I")
            previous_path_identity = None
            query = """
                SELECT path_string.identity - 1,
                       representative_string.identity - 1, p.path_scope,
                       clock_string.identity - 1, p.target_period_ns,
                       p.cone_bound, model_string.identity - 1,
                       source_string.identity - 1, fpga_string.identity - 1
                  FROM path_metadata AS p
                  JOIN string_identity AS path_string
                    ON path_string.value = p.path
                  JOIN string_identity AS representative_string
                    ON representative_string.value = p.representative_path
             LEFT JOIN string_identity AS clock_string
                    ON clock_string.value = p.clock_domain
             LEFT JOIN string_identity AS model_string
                    ON model_string.value = p.interconnect_delay_model
             LEFT JOIN string_identity AS source_string
                    ON source_string.value = p.optimization_source
             LEFT JOIN string_identity AS fpga_string
                    ON fpga_string.value = p.optimization_fpga
              ORDER BY p.path
            """
            for row in connection.execute(query):
                if previous_path_identity == row[0]:
                    raise ValidationError("global STA local/crossing path overlap")
                previous_path_identity = row[0]
                path_indices.append(row[0])
                representative_indices.append(row[1])
                scopes.append(row[2])
                clock_indices.append(
                    _UINT32_MAX if row[3] is None else row[3]
                )
                target_periods.append(row[4])
                cone_bounds.append(row[5])
                model_indices.append(
                    _UINT32_MAX if row[6] is None else row[6]
                )
                optimization_sources.append(_UINT32_MAX if row[7] is None else row[7])
                optimization_fpgas.append(_UINT32_MAX if row[8] is None else row[8])
            has_eliminations = any(v != _UINT32_MAX for v in optimization_sources)
            return _IndexedPathMetadata(
                strings,
                path_indices,
                representative_indices,
                scopes,
                clock_indices,
                target_periods,
                cone_bounds,
                model_indices,
                optimization_sources=optimization_sources if has_eliminations else None,
                optimization_fpgas=optimization_fpgas if has_eliminations else None,
                validated=True,
            )
        finally:
            self._pending.clear()
            self._close()


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
        paths: Sequence[str],
        events: Sequence[str],
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

    @property
    def packed_identity_bytes(self) -> int | None:
        """Bytes in packed path/event pools, if owned by this typed view."""

        if not isinstance(self._paths, _PackedUtf8Strings) or not isinstance(
            self._events, _PackedUtf8Strings
        ):
            return None
        return self._paths.packed_bytes + self._events.packed_bytes


class _EventCheckBuilder:
    """Append checks with bounded Python identity and compression state."""

    _EVENT_CACHE_SIZE = 8192
    _BUNDLE_CACHE_SIZE = 4096
    _BUNDLE_CANDIDATE_CACHE_SIZE = 8
    _INDEX_COMMIT_ROWS = 4096

    def __init__(self):
        self._finished = False
        self._path_payload = bytearray()
        self._path_offsets = array("Q", [0])
        self._event_payload = bytearray()
        self._event_offsets = array("Q", [0])
        self._event_cache: OrderedDict[str, int] = OrderedDict()
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
        self._bundle_cache: OrderedDict[
            tuple[bytes, int], tuple[int, ...]
        ] = OrderedDict()
        self._current_path_index: int | None = None
        self._current_path: str | None = None
        self._current_bundle_start = 0
        self._current_identities: set[tuple[int, str]] = set()
        self._target_chain: tuple[float, tuple[float, ...]] | None = None
        self._runtime_chain: tuple[float, tuple[float, ...]] | None = None
        descriptor, name = tempfile.mkstemp(
            prefix="emuflow-global-sta-checks-", suffix=".sqlite3"
        )
        os.close(descriptor)
        self._database_path = Path(name)
        self._connection: sqlite3.Connection | None = sqlite3.connect(name)
        self._connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-8192;
            CREATE TABLE seen_path (
                path TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TABLE representative_bundle (
                bundle_hash BLOB NOT NULL,
                check_count INTEGER NOT NULL,
                representative_start INTEGER NOT NULL,
                PRIMARY KEY(bundle_hash, representative_start)
            ) WITHOUT ROWID;
            CREATE INDEX representative_bundle_lookup
                ON representative_bundle(bundle_hash, check_count);
            """
        )
        self._pending_index_rows = 0

    def _close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()
        self._database_path.unlink(missing_ok=True)

    def _index_mutated(self) -> None:
        self._pending_index_rows += 1
        if self._pending_index_rows >= self._INDEX_COMMIT_ROWS:
            assert self._connection is not None
            self._connection.commit()
            self._pending_index_rows = 0

    def __del__(self):
        try:
            self._close()
        except BaseException:
            pass

    @staticmethod
    def _append_packed(value: str, payload: bytearray, offsets: array) -> int:
        identity = len(offsets) - 1
        if identity >= _UINT32_MAX:
            raise ValidationError(
                "global STA indexed identity population is too large"
            )
        payload.extend(value.encode("utf-8"))
        offsets.append(len(payload))
        return identity

    def _intern_event(self, event: str) -> int:
        identity = self._event_cache.get(event)
        if identity is None:
            identity = self._append_packed(
                event, self._event_payload, self._event_offsets
            )
            self._event_cache[event] = identity
            if len(self._event_cache) > self._EVENT_CACHE_SIZE:
                self._event_cache.popitem(last=False)
        else:
            self._event_cache.move_to_end(event)
        return identity

    def _rows_numerically_equal(
        self, left: int, right: int
    ) -> bool:
        if (
            self._roles[left] != self._roles[right]
            or self._launches[left] != self._launches[right]
            or self._required[left] != self._required[right]
        ):
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

    def _bundles_numerically_equal(
        self, representative_start: int, begin: int, count: int
    ) -> bool:
        if representative_start + count > len(self._representative_indices):
            raise ValidationError(
                "global STA representative bundle index is malformed"
            )
        return all(
            self._rows_numerically_equal(
                self._representative_indices[representative_start + offset],
                begin + offset,
            )
            for offset in range(count)
        )

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

        start = None
        check_count = end - begin
        hash_bytes = bundle_hash.to_bytes(8, "little")
        cache_key = (hash_bytes, check_count)
        candidates = self._bundle_cache.get(cache_key, ())
        for candidate_start in candidates:
            if self._bundles_numerically_equal(
                candidate_start, begin, check_count
            ):
                start = candidate_start
                break
        if start is None:
            assert self._connection is not None
            for (candidate_start,) in self._connection.execute(
                    """
                    SELECT representative_start
                      FROM representative_bundle
                     WHERE bundle_hash=? AND check_count=?
                  ORDER BY representative_start
                    """,
                    (sqlite3.Binary(hash_bytes), check_count),
                ):
                if candidate_start in candidates:
                    continue
                if self._bundles_numerically_equal(
                    candidate_start, begin, check_count
                ):
                    start = candidate_start
                    break
        if start is None:
            start = len(self._representative_indices)
            if start + check_count >= _UINT32_MAX:
                raise ValidationError(
                    "global STA representative population exceeds indexed storage"
                )
            self._representative_indices.extend(range(begin, end))
            assert self._connection is not None
            self._connection.execute(
                "INSERT INTO representative_bundle VALUES (?, ?, ?)",
                (sqlite3.Binary(hash_bytes), check_count, start),
            )
            self._index_mutated()
        updated = (start, *(value for value in candidates if value != start))[
            : self._BUNDLE_CANDIDATE_CACHE_SIZE
        ]
        self._bundle_cache[cache_key] = updated
        self._bundle_cache.move_to_end(cache_key)
        if len(self._bundle_cache) > self._BUNDLE_CACHE_SIZE:
            self._bundle_cache.popitem(last=False)
        for offset, index in enumerate(range(begin, end)):
            self._original_to_representative[index] = start + offset

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
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(event, str)
            or not event
        ):
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
        role_index = _EVENT_CHECK_ROLE_INDEX[role]
        if path != self._current_path:
            self._finish_bundle()
            assert self._connection is not None
            try:
                self._connection.execute(
                    "INSERT INTO seen_path VALUES (?)", (path,)
                )
                self._index_mutated()
            except sqlite3.IntegrityError as error:
                raise ValidationError(
                    "global STA indexed checks are not path-contiguous"
                ) from error
            path_index = self._append_packed(
                path, self._path_payload, self._path_offsets
            )
            self._current_path_index = path_index
            self._current_path = path
            self._current_identities = set()
            self._target_chain = None
            self._runtime_chain = None
            self._current_bundle_start = len(self._roles)
        assert self._current_path_index is not None
        event_index = self._intern_event(event)
        identity = (role_index, event)
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
        self._path_indices.append(self._current_path_index)
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
        try:
            self._finish_bundle()
            self._finished = True
            paths = _PackedUtf8Strings(
                self._path_payload, self._path_offsets
            )
            events = _PackedUtf8Strings(
                self._event_payload, self._event_offsets
            )
            result = _IndexedEventChecks(
                paths,
                events,
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
            self._event_cache.clear()
            self._bundle_cache.clear()
            return result
        finally:
            self._close()


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


def _validate_indexed_checks(rows: _IndexedEventChecks) -> None:
    """Validate canonical typed rows without reconstructing every dataclass."""

    completed_paths: set[int] = set()
    current_path = None
    current_identities: set[tuple[int, int]] = set()
    target = None
    runtime = None

    def finish_bundle() -> None:
        if current_path is None:
            return
        if target is None and runtime is None:
            raise ValidationError("global STA event has no original-path observation")
        if target is None or runtime is None:
            raise ValidationError("global STA target/runtime population disagrees")
        if not rows._physical_chain_equal(target, runtime):
            raise ValidationError(
                "global STA target/runtime physical chains disagree"
            )
        completed_paths.add(current_path)

    for index in range(len(rows)):
        rows._validate_row(index)
        path = rows._path_indices[index]
        if path != current_path:
            finish_bundle()
            if path in completed_paths:
                raise ValidationError("global STA requires contiguous original-path bundles")
            current_path = path
            current_identities = set()
            target = None
            runtime = None
        role = rows._roles[index]
        identity = (role, rows._events[rows._event_indices[index]])
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
    finish_bundle()


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
    completed_paths = set()
    current_path = None
    current_identities = set()
    target = None
    runtime = None

    def finish_bundle() -> None:
        if current_path is None:
            return
        if target is None and runtime is None:
            raise ValidationError("global STA event has no original-path observation")
        if target is None or runtime is None:
            raise ValidationError("global STA target/runtime population disagrees")
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")
        completed_paths.add(current_path)

    for row in rows:
        _validate_check_values(row)
        if row.path != current_path:
            finish_bundle()
            if row.path in completed_paths:
                raise ValidationError("global STA requires contiguous original-path bundles")
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
    finish_bundle()
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

    # Keep only the active original-path bundle; interleaved input is invalid.
    seen_paths = set()
    current_path = None
    current_indices: list[int] = []
    path_identities = (
        enumerate(rows._path_indices)
        if isinstance(rows, _IndexedEventChecks)
        else ((index, row.path) for index, row in enumerate(rows))
    )
    for index, path_identity in path_identities:
        if path_identity != current_path:
            if path_identity in seen_paths:
                raise ValidationError("global STA requires contiguous original-path bundles")
            if current_indices:
                add_bundle(current_indices)
            current_path = path_identity
            seen_paths.add(path_identity)
            current_indices = []
        current_indices.append(index)
    if current_indices:
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
        if "launcher_sha256" in engine:
            log.write(f"EmuFlow OpenSTA launcher SHA256 {engine['launcher_sha256']}\n")
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
    launcher_sha256 = None
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
            launcher_seal = re.fullmatch(
                r"EmuFlow OpenSTA launcher SHA256 ([0-9a-f]{64})\n?", line
            )
            if launcher_seal:
                launcher_sha256 = launcher_seal[1]
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
            if launcher_sha256 is not None:
                result["launcher_sha256"] = launcher_sha256
            return result
        if executable_sha256 is not None:
            return {
                "name": "OpenSTA",
                "version": version,
                "revision": f"binary-sha256:{executable_sha256}",
                "reported_revision": reported_revision,
                "executable_sha256": executable_sha256,
                **({"launcher_sha256": launcher_sha256} if launcher_sha256 is not None else {}),
            }
    raise ValidationError("global OpenSTA log lacks engine version/revision")


def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    """Bind canonical measurements to event checks without composing delays.

Shared database readers validate identities only. This exporter does not call
the Python event propagator or consume its per-path numerical results.
"""
    from .physical_timing_inputs import (
        _board_link_delay_database, _endpoint_delay_database,
        _logic_segment_database,
    )
    from .tdm import (
        is_fixed_slot_transport_schedule,
        open_tdm_schedule_timing_path_stream,
    )

    if not is_fixed_slot_transport_schedule(schedule):
        raise ValidationError("global OpenSTA requires fixed-slot transport")
    if any(link.word_timing is not None for link in platform.links):
        raise ValidationError("periodic LVDS word physical event projection is not yet qualified; ratio estimates cannot substitute for execution events")
    for field in ("logic_segment_timing", "local_path_timing", "boundary_timing"):
        databases = physical.get(field)
        if isinstance(databases, Mapping) and any(
            isinstance(database, Mapping) and "sampling_cost_policy" in database
            for database in databases.values()
        ):
            raise ValidationError("sampling-cost physical evidence requires clock-aware event binding; the legacy binder cannot discard clock/check adjustments")
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
                    eliminated_segments = [s for s in chain if validate_optimization_record(s)]
                    eliminated_interfaces = []
                    for hop in record["scheduled_hops"]:
                        for key in ("tx_endpoint", "rx_endpoint"):
                            endpoint = endpoints.get(hop[key])
                            if isinstance(endpoint, Mapping) and validate_optimization_record(endpoint):
                                require_optimization_proof(physical, endpoint["optimization_fpga"], endpoint["optimization_source"])
                                eliminated_interfaces.append(endpoint)
                    for segment in eliminated_segments:
                        require_optimization_proof(physical, segment["fpga"], segment["optimization_source"], segment)
                    if eliminated_segments or eliminated_interfaces:
                        # Consume and validate the complete original chain even when
                        # a proven absent dependency makes numerical checks inapplicable.
                        seen_entries = set()
                        periods = set()
                        for k, demand in enumerate(cut_demands):
                            hops = by_demand.get(demand, [])
                            if (not hops or chain[k]["replace_tx_endpoint"] != hops[0]["tx_endpoint"]
                                    or chain[k]["kind"] != ("launch" if k == 0 else "transition")):
                                raise ValidationError("global STA eliminated cut/segment binding disagrees")
                            for hop in hops:
                                eid = hop["schedule_entry"]
                                if eid in seen_entries or hop["tx_endpoint"] not in endpoints or hop["rx_endpoint"] not in endpoints:
                                    raise ValidationError("global STA eliminated path has incomplete endpoint/event coverage")
                                seen_entries.add(eid)
                                periods.add(float(hop["tdm_slot_ns"]))
                        if (chain[-1]["kind"] != "capture" or chain[-1]["replace_tx_endpoint"] is not None
                                or len(periods) != 1 or len(seen_entries) != len(record["scheduled_hops"])):
                            raise ValidationError("global STA eliminated path has incomplete segment/event coverage")
                        proof_segment = eliminated_segments[0] if eliminated_segments else eliminated_interfaces[0]
                        proof_fpga = proof_segment.get("fpga",proof_segment.get("optimization_fpga"))
                        path_metadata.append(member, representative_path=record["path"], path_scope="cross-fpga",
                            clock_domain=record.get("clock_domain"), target_period_ns=record["clock_period_ns"],
                            physical_logic_segments_cone_bound=False, interconnect_delay_model=None,
                            optimization_source=proof_segment["optimization_source"], optimization_fpga=proof_fpga)
                        continue
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
                            s.get("measurement") in {"cut-net-cone-upper-bound", "optimized-endpoint-collection-upper-bound"}
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
                            if "fixed_slot_tx_time_ns" not in hop:
                                raise ValidationError(
                                    "global STA timing stream lacks concrete scheduled TX time"
                                )
                            tx = float(hop["fixed_slot_tx_time_ns"])
                            ratio = hop.get("tdm_ratio")
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
            eliminated = validate_optimization_record(path)
            if eliminated:
                require_optimization_proof(physical, fpga, path["optimization_source"], path)
            path_metadata.append(
                path["id"],
                representative_path=path["id"],
                path_scope="same-fpga-local",
                clock_domain=path.get("clock_domain"),
                target_period_ns=path["clock_period_ns"],
                physical_logic_segments_cone_bound=(
                    not eliminated and path.get("measurement") != "explicit-routed-path-chain"
                ),
                optimization_source=path["optimization_source"] if eliminated else None,
                optimization_fpga=fpga if eliminated else None,
            )
            if eliminated:
                continue
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
    rows = result.finish()
    if rows:
        rows = validate_checks(rows)
    indexed_path_metadata = path_metadata.finish()
    if not rows and (not indexed_path_metadata or any(not is_optimized_away(info) for info in indexed_path_metadata.values())):
        raise ValidationError("global STA has no checks for an implemented original path")
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


_RESULT_SCALARS = ("system_delay_bound_ns", "target_required_time_ns",
                   "runtime_required_time_ns", "target_clock_slack_bound_ns",
                   "runtime_clock_slack_bound_ns")


def build_opensta_timing(runtime, metadata, measurements):
    """Build the canonical report directly from engine scalars, never Python timing.

    Binding owns coverage and physical qualifications. OpenSTA owns numerical
    propagation. Python only aggregates the project's original-path metric.
    """
    metadata_paths = metadata["paths"]
    if not metadata_paths:
        raise ValidationError("global STA empty original population")
    implemented_count = sum(not is_optimized_away(info) for info in metadata_paths.values())

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
                raise ValidationError(
                    "global STA requires complete, contiguous original-path measurements"
                )
            info = metadata_paths[current_path]
            target = current_rows["target"]
            runtime_row = current_rows["runtime"]
            if typed_builder is not None:
                if not typed_builder.append(current_path, target, runtime_row):
                    raise ValidationError(
                        "global STA requires complete, contiguous original-path measurements"
                    )
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
                or is_optimized_away(metadata_paths[path_id])
                or role not in {"target", "runtime", "tx", "commit"}
                or not isinstance(event, str)
                or not event
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            if path_id != current_path:
                if completed is not None and path_id in completed:
                    raise ValidationError(
                        "global STA requires complete, contiguous original-path measurements"
                    )
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
            if len(paths) != implemented_count:
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            paths.sort(key=lambda path: path["path"])
        return paths, failures, checks

    paths, failures, measurement_count = ordered_paths()
    if not isinstance(paths, _IndexedSystemTimingPaths):
        for path_id, info in metadata_paths.items():
            if is_optimized_away(info):
                validate_optimization_record(info)
                paths.append({"path": path_id, **info, **dict.fromkeys(_RESULT_SCALARS)})
        paths.sort(key=lambda path: path["path"])
    source = metadata["source_binding"]
    scope = "whole-original-design" if source is not None else "cross-fpga-path-subset"
    local = 0
    bounds = 0
    maximum_delay = None
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
        if is_optimized_away(path):
            continue
        bounds += path["physical_logic_segments_cone_bound"]
        maximum_delay = path["system_delay_bound_ns"] if maximum_delay is None else max(maximum_delay, path["system_delay_bound_ns"])
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
        if implemented_count == 0:
            metrics[role]["wns_ns"] = None
        metrics[role]["original_path_tns_ns"] = math.fsum(
            negative_partials[role]
        )
    timing = {"schema": "emuflow.system-timing/v2", "status": "fail" if failures else "pass",
              "qualification": "opensta-fixed-event-physical-bounds", "timing_scope": scope,
              "source_binding": source, "paths": paths, "path_population_policy": PATH_POPULATION_POLICY,
              "path_exactness": {"physical_model": "routed-staging-chain-upper-bounds",
                  "endpoint_exact_logic_paths": implemented_count-bounds, "cone_bound_logic_paths": bounds,
                  "fallback_logic_paths": 0, "discontinuous_compressed_paths": 0,
                  "physical_logic_segment_bounds": True, "fixed_slot_event_failures": failures},
              "summary": {"timing_paths": len(paths), "implemented_paths": implemented_count, "optimized_away_paths": len(paths)-implemented_count, "original_paths": source["original_paths"] if source else None,
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
    # A raw arrival bound does not identify the launch/capture edge budget.
    # Its reciprocal is not a safe operating frequency (even a half-cycle
    # check is a counterexample). The current binding does not retain an
    # affine clock budget, so leave these claims unavailable until that
    # source-bound contract and its physical margins are supplied.
    timing["runtime_clock"].update(
        minimum_safe_period_bound_ns=None,
        maximum_safe_frequency_bound_mhz=None,
        operating_frequency_status="unavailable-missing-edge-and-physical-margin-budget",
    )
    if metrics["runtime"]["wns_ns"] is not None and metrics["runtime"]["wns_ns"] < 0:
        timing["status"] = "fail"
    timing["global_opensta"] = {"schema": "emuflow.global-opensta-check/v1",
        "authority": "opensta" if measurement_count else "no-numerical-checks",
        "execution": "standalone" if measurement_count else "not-applicable-no-implemented-original-paths", "status": timing["status"],
        "timing_scope": scope, "original_paths": len(paths), "implemented_paths": implemented_count,
        "optimized_away_paths": len(paths)-implemented_count, "path_population_policy": PATH_POPULATION_POLICY, "checks": measurement_count,
        "event_failures": failures, "metrics": metrics,
        "tns_definition": "sum-negative-slack-once-per-implemented-original-TimingPathDB-path"}
    return timing
