"""Path-, topology-, and TDM-pressure-aware Phase 3 reference model.

This module is intentionally the exhaustive Python specification.  It is used
for compact correctness tests and for checking the native scalable refiner; it
is not the large-design production optimizer.
"""

from __future__ import annotations

import gc
import hashlib
import heapq
import itertools
import json
import math
import mmap
import os
import sqlite3
import struct
import subprocess
import tempfile
import uuid
import zlib
from array import array
from collections.abc import Iterator, Sequence
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import ValidationError
from .emuir_storage import open_emuir_sqlite_read_only
from .ir import EMUIR_SCHEMA, VALID_CUT_CLASSES, EmuIR
from .io import read_json, write_json
from .partition import (
    DEFAULT_PATRON_ALGORITHM_VERSION,
    CUT_MODE_STATIC_EXACT,
    _PackedClusterAssignment,
    active_partition_fpgas,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from .platform import Platform
from .bank_allocation import apply_bank_domain_ratios
from .native_tools import is_unified_native_executable, resolve_native_stage_command
from .partition_physical_feedback import (
    PARTITION_PHYSICAL_FEEDBACK_SCHEMA,
    validate_partition_physical_feedback_seal,
)
from .partition_limits import planning_resource_capacities
from .phase3_runtime import phase3_runtime_call
from .resources import RESOURCE_FIELDS
from .routing import (
    build_directed_graph,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
    route_link_delay_coefficients_ns,
    route_hop_delay_ns,
)
from .sta import (
    STA_PATH_DATABASE_INDEX_SCHEMA,
    STA_PATH_DATABASE_SQLITE_FORMAT,
    _normalized_slack,
    _validate_database_normalization,
    iter_sta_path_database_paths,
    sta_path_database_payload_path,
)


PARTITION_PRESSURE_MODEL_SCHEMA = "emuflow.partition-pressure-model/v6"
PARTITION_PRESSURE_TRACE_SCHEMA = "emuflow.partition-pressure-trace/v6"
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7 = (
    "emuflow.partition-pressure-trace/v7"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8 = (
    "emuflow.partition-pressure-trace/v8"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9 = (
    "emuflow.partition-pressure-trace/v9"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA = "emuflow.partition-pressure-trace/v10"
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11 = (
    "emuflow.partition-pressure-trace/v11"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V14 = (
    "emuflow.partition-pressure-trace/v14"
)
_PATRON_TYPED_OUTPUT_MAGIC = 0x3154554F54415045
_PATRON_TYPED_OUTPUT_VERSION = 1

PATRON_FLOW_REFINEMENT_ALGORITHM_V1 = (
    "flowcutter-bidirectional-piercing-v1"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V2 = (
    "flowcutter-bidirectional-piercing-frontier-closure-v2"
)
PATRON_FLOW_REFINEMENT_ALGORITHM = (
    "flowcutter-bidirectional-piercing-physical-hop-guard-v4"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V5 = (
    "flowcutter-bidirectional-piercing-endpoint-residual-v5"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V8 = (
    "flowcutter-ranked-frontier-predictive-timing-v8"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V3 = (
    "flowcutter-bidirectional-piercing-ranked-frontier-closure-v3"
)
PATRON_FLOW_CORRIDOR_DISTANCE = 4
PATRON_FLOW_PIERCING_STRATEGY = 0
PATRON_FLOW_MAX_LEGAL_CANDIDATES = 8
PATRON_FLOW_MAX_POLISH_MOVES = 512
PATRON_FLOW_MAX_FRONTIER_PATHS = 256
PATRON_FLOW_MAX_TAIL_MOVES_V2 = 16
PATRON_FLOW_MAX_TAIL_MOVES = 256
PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS = 5.0
PARTITION_PRESSURE_REPORT_SCHEMA = "emuflow.partition-pressure-report/v6"
PARTITION_PRESSURE_PROVIDER = "patron-endpoint-exact-reference-v6"
PARTITION_PRESSURE_NATIVE_PROVIDER = "patron-endpoint-exact-native-v6"
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7 = (
    "patron-endpoint-exact-flow-native-v7"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8 = (
    "patron-endpoint-exact-flow-native-v8"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9 = (
    "patron-endpoint-exact-flow-native-v9"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER = (
    "patron-endpoint-exact-flow-native-v10"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11 = (
    "patron-endpoint-exact-flow-native-v11"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V14 = (
    "patron-predictive-timing-flow-native-v14"
)
GAIN_QUANTUM = 1.0e-9
BOUNDARY_FANOUT_PENALTY_SCALE_NS = 0.0
MAX_SCALABLE_SWEEPS = 4
SCALABLE_EJECTION_CRITICAL_LIMIT = 2048
SCALABLE_EJECTION_DONOR_LIMIT = 32
MAX_SCALABLE_EJECTIONS = 64


class _PackedStringOrdinalIndex:
    """Sorted UTF-8 identities with fixed-width ordinal payloads.

    A production partition can contain millions of cells and nets.  Python
    dictionaries for those identities cost several pointers and allocator
    objects per record.  This index retains each identity once in a byte pool
    and uses binary search over fixed-width offsets.  SQLite supplies the
    canonical ordering, so construction does not need a graph-sized sort
    vector either.
    """

    def __init__(self) -> None:
        self._data = bytearray()
        self._offsets = array("Q", [0])
        self._ordinals = array("Q")

    def append(self, identity: str, ordinal: int) -> None:
        if (
            not isinstance(identity, str)
            or not identity
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal >= 2**64
        ):
            raise ValidationError("partition pressure identity index is invalid")
        encoded = identity.encode("utf-8")
        if self._ordinals and encoded <= self._key_bytes(len(self) - 1):
            raise ValidationError(
                "partition pressure identity ordering is invalid"
            )
        self._data.extend(encoded)
        self._offsets.append(len(self._data))
        self._ordinals.append(ordinal)

    def __len__(self) -> int:
        return len(self._ordinals)

    def _key_bytes(self, index: int) -> bytes:
        return bytes(
            self._data[self._offsets[index] : self._offsets[index + 1]]
        )

    def key_at(self, index: int) -> str:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure identity index must be integer")
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self._key_bytes(index).decode("utf-8")

    def ordinal_at(self, index: int) -> int:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure identity index must be integer")
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return int(self._ordinals[index])

    def get(self, identity: Any, default: Any = None) -> Any:
        position = self.position(identity)
        return default if position is None else int(self._ordinals[position])

    def position(self, identity: Any) -> Optional[int]:
        if not isinstance(identity, str):
            return None
        encoded = identity.encode("utf-8")
        left = 0
        right = len(self)
        while left < right:
            middle = (left + right) // 2
            observed = self._key_bytes(middle)
            if observed < encoded:
                left = middle + 1
            else:
                right = middle
        if left >= len(self) or self._key_bytes(left) != encoded:
            return None
        return left

    def __contains__(self, identity: Any) -> bool:
        return self.get(identity) is not None

    def __getitem__(self, identity: str) -> int:
        result = self.get(identity)
        if result is None:
            raise KeyError(identity)
        return int(result)

    @property
    def payload_bytes(self) -> int:
        return (
            len(self._data)
            + self._offsets.itemsize * len(self._offsets)
            + self._ordinals.itemsize * len(self._ordinals)
        )


class _PackedIndexKeys(list):
    """Read-only logical list backed by a packed identity index."""

    def __init__(self, index: _PackedStringOrdinalIndex) -> None:
        super().__init__()
        self._index = index

    def __len__(self) -> int:
        return len(self._index)

    def __iter__(self) -> Iterator[str]:
        return (self._index.key_at(index) for index in range(len(self)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._index.key_at(item) for item in range(*index.indices(len(self)))]
        return self._index.key_at(index)

    def _immutable(self, *args, **kwargs):
        raise TypeError("partition pressure identity sequence is immutable")

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


class _PackedIndexPositions(Mapping[str, int]):
    """Mapping from a packed key to its lexical position."""

    def __init__(self, index: _PackedStringOrdinalIndex) -> None:
        self._index = index

    def __getitem__(self, key: str) -> int:
        position = self._index.position(key)
        if position is None:
            raise KeyError(key)
        return position

    def __iter__(self) -> Iterator[str]:
        return (self._index.key_at(index) for index in range(len(self._index)))

    def __len__(self) -> int:
        return len(self._index)


class _NativeClusterSequence(Sequence):
    """Repeatable cluster records in native lexical identity order."""

    def __init__(
        self,
        records: Sequence[Mapping[str, Any]],
        identities: _PackedStringOrdinalIndex,
    ) -> None:
        self._records = records
        self._identities = identities

    def __len__(self) -> int:
        return len(self._identities)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        return self._records[self._identities.ordinal_at(index)]


class _PackedPressureClusterSequence(list):
    """Lazy pressure-cluster projection over the authoritative artifact."""

    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self._records = records

    def __len__(self) -> int:
        return len(self._records)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        cluster = self._records[index]
        return {
            "cluster": cluster["id"],
            "cells": len(cluster["instances"]),
            "resources": dict(sorted(cluster["resources"].items())),
            "fixed_fpga": cluster["fixed_fpga"],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return (self._record(index) for index in range(len(self)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure cluster index must be integer")
        return self._record(index)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Sequence):
            return False
        return len(self) == len(other) and all(
            left == right for left, right in zip(self, other)
        )

    def _immutable(self, *args, **kwargs):
        raise TypeError("partition pressure cluster sequence is immutable")

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


class _PackedPressureNetSequence(list):
    """Canonical pressure nets backed by compact integer membership arrays."""

    _CUT_CLASSES = (
        "clock",
        "combinational",
        "multi_driver",
        "primary_input",
        "register_input",
        "register_output",
        "reset",
        "undriven",
    )
    _CUT_CLASS_INDEX = {name: index for index, name in enumerate(_CUT_CLASSES)}

    def __init__(
        self,
        cluster_records: Sequence[Mapping[str, Any]],
        native_cluster_identities: _PackedStringOrdinalIndex,
    ) -> None:
        super().__init__()
        self._cluster_records = cluster_records
        self.native_cluster_identities = native_cluster_identities
        self.construction_identity_bytes = 0
        if len(native_cluster_identities) >= 2**32:
            raise ValidationError(
                "partition pressure cluster count is unsupported"
            )
        self._artifact_to_native = array("I", [0]) * len(cluster_records)
        for native_ordinal in range(len(native_cluster_identities)):
            artifact_ordinal = native_cluster_identities.ordinal_at(
                native_ordinal
            )
            if artifact_ordinal >= len(cluster_records):
                raise ValidationError(
                    "partition pressure cluster identity is invalid"
                )
            self._artifact_to_native[artifact_ordinal] = native_ordinal
        self._net_identities = _PackedStringOrdinalIndex()
        self._cut_classes = array("B")
        self._driver_offsets = array("Q", [0])
        self._drivers = array("I")
        self._sink_offsets = array("Q", [0])
        self._sinks = array("I")

    @staticmethod
    def _append_members(
        target: array, values: Iterable[int], cluster_count: int
    ) -> None:
        previous = -1
        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value == previous
                or value < 0
                or value >= 2**32
                or value >= cluster_count
            ):
                raise ValidationError(
                    "partition pressure net membership is invalid"
                )
            target.append(value)
            previous = value

    def append_record(
        self,
        net: str,
        cut_class: str,
        drivers: Iterable[int],
        sinks: Iterable[int],
    ) -> None:
        cut_index = self._CUT_CLASS_INDEX.get(cut_class)
        if cut_index is None:
            raise ValidationError("partition pressure net class is invalid")
        self._net_identities.append(net, len(self._cut_classes))
        driver_start = len(self._drivers)
        sink_start = len(self._sinks)
        self._append_members(self._drivers, drivers, len(self._cluster_records))
        self._append_members(self._sinks, sinks, len(self._cluster_records))
        if len(self._drivers) == driver_start or len(self._sinks) == sink_start:
            raise ValidationError("partition pressure net endpoints are invalid")
        self._cut_classes.append(cut_index)
        self._driver_offsets.append(len(self._drivers))
        self._sink_offsets.append(len(self._sinks))

    def __len__(self) -> int:
        return len(self._cut_classes)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return {
            "net": self._net_identities.key_at(index),
            "cut_class": self._CUT_CLASSES[self._cut_classes[index]],
            "drivers": [
                self._cluster_records[ordinal]["cluster"]
                for ordinal in self._drivers[
                    self._driver_offsets[index] : self._driver_offsets[index + 1]
                ]
            ],
            "sinks": [
                self._cluster_records[ordinal]["cluster"]
                for ordinal in self._sinks[
                    self._sink_offsets[index] : self._sink_offsets[index + 1]
                ]
            ],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return (self._record(index) for index in range(len(self)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure net index must be integer")
        return self._record(index)

    def get(self, net: Any, default: Any = None) -> Any:
        index = self._net_identities.get(net)
        return default if index is None else self._record(index)

    def ordinal(self, net: Any) -> Optional[int]:
        result = self._net_identities.get(net)
        return None if result is None else int(result)

    def native_net_members(
        self, index: int
    ) -> Tuple[Sequence[int], Sequence[int]]:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure net index must be integer")
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return (
            self._drivers[
                self._driver_offsets[index] : self._driver_offsets[index + 1]
            ],
            self._sinks[
                self._sink_offsets[index] : self._sink_offsets[index + 1]
            ],
        )

    def native_cluster_ordinal(self, artifact_ordinal: int) -> int:
        return int(self._artifact_to_native[artifact_ordinal])

    def native_clusters(self) -> _NativeClusterSequence:
        return _NativeClusterSequence(
            self._cluster_records, self.native_cluster_identities
        )

    def native_cluster_names(self) -> _PackedIndexKeys:
        return _PackedIndexKeys(self.native_cluster_identities)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Sequence):
            return False
        return len(self) == len(other) and all(
            left == right for left, right in zip(self, other)
        )

    def _immutable(self, *args, **kwargs):
        raise TypeError("partition pressure net sequence is immutable")

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

    @property
    def payload_bytes(self) -> int:
        """Bytes in packed net identities, memberships, and ordinal maps.

        This deliberately excludes the final cluster/path records and Python
        object overhead.  It is a narrow accounting metric for the compact
        net payload that replaces decoded per-net dictionaries.
        """
        return (
            self.native_cluster_identities.payload_bytes
            + self._net_identities.payload_bytes
            + self._artifact_to_native.itemsize * len(self._artifact_to_native)
            + self._cut_classes.itemsize * len(self._cut_classes)
            + self._driver_offsets.itemsize * len(self._driver_offsets)
            + self._drivers.itemsize * len(self._drivers)
            + self._sink_offsets.itemsize * len(self._sink_offsets)
            + self._sinks.itemsize * len(self._sinks)
        )


class _PackedPressurePathSequence(list):
    """Timing paths stored as fixed-width source/net/cluster ordinals."""

    _TRANSITION_MODELS = (
        "conservative-net-worst-v1",
        "endpoint-exact-reverse-chain-v1",
    )

    def __init__(
        self,
        known_nets: _PackedStringOrdinalIndex,
        pressure_nets: _PackedPressureNetSequence,
        clusters: Sequence[Mapping[str, Any]],
    ) -> None:
        super().__init__()
        self._known_nets = known_nets
        self._pressure_nets = pressure_nets
        self._clusters = clusters
        missing = 2**64 - 1
        self._source_net_positions = array("Q", [missing]) * len(known_nets)
        for position in range(len(known_nets)):
            ordinal = known_nets.ordinal_at(position)
            if (
                ordinal >= len(known_nets)
                or self._source_net_positions[ordinal] != missing
            ):
                raise ValidationError(
                    "partition pressure net identity domain is invalid"
                )
            self._source_net_positions[ordinal] = position
        if missing in self._source_net_positions:
            raise ValidationError(
                "partition pressure net identity domain is incomplete"
            )
        self._path_data = bytearray()
        self._path_offsets = array("Q", [0])
        self._clock_data = bytearray()
        self._clock_offsets = array("Q", [0])
        self._clock_present = bytearray()
        self._periods = array("d")
        self._slacks = array("d")
        self._source_net_offsets = array("Q", [0])
        self._source_nets = array("Q")
        self._pressure_net_offsets = array("Q", [0])
        self._pressure_net_ordinals = array("I")
        self._transition_models = bytearray()
        self._start_clusters = array("q")
        self._end_clusters = array("q")
        self.indexed_sql_projection = False

    @staticmethod
    def _append_string(
        data: bytearray, offsets: array, value: str
    ) -> None:
        data.extend(value.encode("utf-8"))
        offsets.append(len(data))

    @staticmethod
    def _string_at(data: bytearray, offsets: array, index: int) -> str:
        return bytes(data[offsets[index] : offsets[index + 1]]).decode(
            "utf-8"
        )

    def append_record(
        self,
        path: str,
        clock_domain: Any,
        period: float,
        slack: float,
        path_nets: Sequence[str],
        start_cluster: Optional[int],
        end_cluster: Optional[int],
        endpoint_exact: bool,
    ) -> None:
        source_net_ordinals = array("Q")
        pressure_net_ordinals = array("I")
        for net in path_nets:
            source_ordinal = self._known_nets.get(net)
            if source_ordinal is None:
                raise ValidationError(
                    "partition pressure path references an unknown net"
                )
            source_net_ordinals.append(source_ordinal)
            pressure_ordinal = self._pressure_nets.ordinal(net)
            if pressure_ordinal is not None:
                pressure_net_ordinals.append(pressure_ordinal)
        self.append_ordinals(
            path,
            clock_domain,
            period,
            slack,
            source_net_ordinals,
            pressure_net_ordinals,
            start_cluster,
            end_cluster,
            endpoint_exact,
        )

    def append_ordinals(
        self,
        path: str,
        clock_domain: Any,
        period: float,
        slack: float,
        source_net_ordinals: Iterable[int],
        pressure_net_ordinals: Iterable[int],
        start_cluster: Optional[int],
        end_cluster: Optional[int],
        endpoint_exact: bool,
    ) -> None:
        if not isinstance(path, str) or not path:
            raise ValidationError("partition pressure path identity is invalid")
        if endpoint_exact:
            if (
                isinstance(start_cluster, bool)
                or not isinstance(start_cluster, int)
                or start_cluster < 0
                or start_cluster >= len(self._clusters)
                or isinstance(end_cluster, bool)
                or not isinstance(end_cluster, int)
                or end_cluster < 0
                or end_cluster >= len(self._clusters)
            ):
                raise ValidationError(
                    "partition pressure exact path endpoints are invalid"
                )
            normalized_start_cluster = start_cluster
            normalized_end_cluster = end_cluster
        else:
            # Conservative paths do not have an authoritative endpoint pair.
            # A top-level port can resolve only one side to a cluster; carrying
            # that partial identity into the native protocol creates an invalid
            # mixed (-1, cluster) pair and misrepresents the transition model.
            normalized_start_cluster = -1
            normalized_end_cluster = -1
        self._append_string(self._path_data, self._path_offsets, path)
        if clock_domain is None:
            self._clock_present.append(0)
            self._clock_offsets.append(len(self._clock_data))
        elif isinstance(clock_domain, str):
            self._clock_present.append(1)
            self._append_string(
                self._clock_data, self._clock_offsets, clock_domain
            )
        else:
            raise ValidationError("partition pressure clock domain is invalid")
        self._periods.append(period)
        self._slacks.append(slack)
        for source_ordinal in source_net_ordinals:
            if source_ordinal < 0 or source_ordinal >= len(self._known_nets):
                raise ValidationError(
                    "partition pressure source-net ordinal is invalid"
                )
            self._source_nets.append(source_ordinal)
        for pressure_ordinal in pressure_net_ordinals:
            if pressure_ordinal < 0 or pressure_ordinal >= len(
                self._pressure_nets
            ):
                raise ValidationError(
                    "partition pressure net ordinal is invalid"
                )
            self._pressure_net_ordinals.append(pressure_ordinal)
        self._source_net_offsets.append(len(self._source_nets))
        self._pressure_net_offsets.append(len(self._pressure_net_ordinals))
        self._transition_models.append(int(endpoint_exact))
        self._start_clusters.append(normalized_start_cluster)
        self._end_clusters.append(normalized_end_cluster)

    def __len__(self) -> int:
        return len(self._periods)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        source_nets = self._source_nets[
            self._source_net_offsets[index] : self._source_net_offsets[index + 1]
        ]
        record: Dict[str, Any] = {
            "path": self._string_at(
                self._path_data, self._path_offsets, index
            ),
            "clock_domain": (
                self._string_at(
                    self._clock_data, self._clock_offsets, index
                )
                if self._clock_present[index]
                else None
            ),
            "clock_period_ns": self._periods[index],
            "base_slack_ns": self._slacks[index],
            "path_nets": [
                self._known_nets.key_at(
                    self._source_net_positions[source_ordinal]
                )
                for source_ordinal in source_nets
            ],
            "transition_model": self._TRANSITION_MODELS[
                self._transition_models[index]
            ],
        }
        start_cluster = self._start_clusters[index]
        end_cluster = self._end_clusters[index]
        if self._transition_models[index]:
            if start_cluster < 0 or end_cluster < 0:
                raise ValidationError(
                    "partition pressure exact path endpoints are invalid"
                )
            record["start_cluster"] = self._clusters[start_cluster]["cluster"]
            record["end_cluster"] = self._clusters[end_cluster]["cluster"]
        return record

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return (self._record(index) for index in range(len(self)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure path index must be integer")
        return self._record(index)

    def native_path(
        self, index: int
    ) -> Tuple[float, float, int, int, Sequence[int]]:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition pressure path index must be integer")
        if index < 0 or index >= len(self):
            raise IndexError(index)
        start_cluster = self._start_clusters[index]
        end_cluster = self._end_clusters[index]
        return (
            self._periods[index],
            self._slacks[index],
            (
                -1
                if start_cluster < 0
                else self._pressure_nets.native_cluster_ordinal(start_cluster)
            ),
            (
                -1
                if end_cluster < 0
                else self._pressure_nets.native_cluster_ordinal(end_cluster)
            ),
            self._pressure_net_ordinals[
                self._pressure_net_offsets[index] :
                self._pressure_net_offsets[index + 1]
            ],
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Sequence):
            return False
        return len(self) == len(other) and all(
            left == right for left, right in zip(self, other)
        )

    def _immutable(self, *args, **kwargs):
        raise TypeError("partition pressure path sequence is immutable")

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

    @property
    def payload_bytes(self) -> int:
        arrays = (
            self._path_offsets,
            self._clock_offsets,
            self._periods,
            self._slacks,
            self._source_net_positions,
            self._source_net_offsets,
            self._source_nets,
            self._pressure_net_offsets,
            self._pressure_net_ordinals,
            self._start_clusters,
            self._end_clusters,
        )
        return (
            len(self._path_data)
            + len(self._clock_data)
            + len(self._clock_present)
            + len(self._transition_models)
            + sum(item.itemsize * len(item) for item in arrays)
        )


_PATRON_TYPED_INPUT_MAGIC = b"EMPATRN1"
_PATRON_TYPED_INPUT_END_MAGIC = b"EMPEND1\0"
_PATRON_TYPED_INPUT_VERSION = 1
_PATRON_TYPED_INPUT_ENDIAN_MARKER = 0x01020304
_PATRON_TYPED_INPUT_BUFFER_BYTES = 1024 * 1024


class _NativeBinaryPipeWriter:
    """Stream the checked typed PATRON protocol in bounded batches."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._published = False
        self._pending = bytearray()
        self._checksum = 0
        self._bytes = 0
        self._records = 0

    def _flush_pending(self) -> None:
        if self._stream is None:
            raise ValidationError("native PATRON input writer is closed")
        if self._pending:
            self._stream.write(self._pending)
            self._pending.clear()

    def _emit(self, format_string: str, *values: Any) -> None:
        payload = struct.pack(format_string, *values)
        self._pending.extend(payload)
        self._checksum = zlib.crc32(payload, self._checksum)
        self._bytes += len(payload)
        if len(self._pending) >= _PATRON_TYPED_INPUT_BUFFER_BYTES:
            self._flush_pending()

    def _emit_u32_values(self, values: Iterable[int]) -> int:
        count = 0
        batch: List[int] = []
        for raw in values:
            value = int(raw)
            if value < 0 or value > 0xFFFFFFFF:
                raise ValidationError("native PATRON typed index is invalid")
            batch.append(value)
            count += 1
            if len(batch) == 4096:
                self._emit(f"<{len(batch)}I", *batch)
                batch.clear()
        if batch:
            self._emit(f"<{len(batch)}I", *batch)
        return count

    def header(
        self,
        *,
        flow_version: int,
        use_physical_feedback: bool,
        counts: Tuple[int, int, int, int, int, int],
        integers: Tuple[int, int, int, int, int, int, int, int, int, int],
        scalars: Tuple[float, float, float, float, float],
        flow_configuration: Tuple[int, int, int, int, int, int, int],
    ) -> None:
        flags = 1
        if flow_version != 6:
            flags |= 2
        if use_physical_feedback:
            flags |= 4
        self._emit(
            "<8sIIII6Q10i5d7i",
            _PATRON_TYPED_INPUT_MAGIC,
            _PATRON_TYPED_INPUT_VERSION,
            _PATRON_TYPED_INPUT_ENDIAN_MARKER,
            flow_version,
            flags,
            *counts,
            *integers,
            *scalars,
            *flow_configuration,
        )
        self._records += 1

    def cap(
        self,
        index: int,
        hard: Sequence[float],
        allowed: Sequence[float],
    ) -> None:
        if len(hard) != len(allowed):
            raise ValidationError("native PATRON capacity dimensions disagree")
        values = [
            value
            for pair in zip(hard, allowed)
            for value in pair
        ]
        self._emit(
            f"<BI{len(values)}d", ord("C"), index, *values
        )
        self._records += 1

    def cluster(
        self,
        index: int,
        current: int,
        fixed: int,
        weights: Sequence[float],
    ) -> None:
        self._emit(
            f"<BIii{len(weights)}d",
            ord("L"),
            index,
            current,
            fixed,
            *weights,
        )
        self._records += 1

    def domain(
        self,
        index: int,
        lanes: int,
        is_sll: bool,
        ratio_delay_slope_ns: float,
        opposite: int,
        banks: int,
        wires: int,
        capacity_bits: int,
    ) -> None:
        self._emit(
            "<BIiBdiiiq",
            ord("D"),
            index,
            lanes,
            int(is_sll),
            ratio_delay_slope_ns,
            opposite,
            banks,
            wires,
            capacity_bits,
        )
        self._records += 1

    def route(
        self,
        source: int,
        sink: int,
        arcs: Optional[Sequence[Tuple[int, float]]],
    ) -> None:
        self._emit(
            "<BIIi",
            ord("R"),
            source,
            sink,
            -1 if arcs is None else len(arcs),
        )
        if arcs is not None:
            for domain, delay_ns in arcs:
                self._emit("<Id", domain, delay_ns)
        self._records += 1

    def net(
        self,
        index: int,
        driver_count: int,
        drivers: Iterable[int],
        sink_count: int,
        sinks: Iterable[int],
    ) -> None:
        self._emit("<BIQ", ord("N"), index, driver_count)
        if self._emit_u32_values(drivers) != driver_count:
            raise ValidationError("native PATRON driver count disagrees")
        self._emit("<Q", sink_count)
        if self._emit_u32_values(sinks) != sink_count:
            raise ValidationError("native PATRON sink count disagrees")
        self._records += 1

    def path(
        self,
        index: int,
        period_ns: float,
        slack_ns: float,
        start_cluster: int,
        end_cluster: int,
        nets: Sequence[int],
        feedback: Optional[Tuple[int, int, float]],
    ) -> None:
        self._emit(
            "<BI2diiQ",
            ord("P"),
            index,
            period_ns,
            slack_ns,
            start_cluster,
            end_cluster,
            len(nets),
        )
        if self._emit_u32_values(nets) != len(nets):
            raise ValidationError("native PATRON path-net count disagrees")
        if feedback is not None:
            self._emit("<iid", *feedback)
        self._records += 1

    def _finish_payload(self) -> None:
        if self._stream is None or self._published:
            raise ValidationError("native PATRON input writer is closed")
        trailer = struct.pack(
            "<8sI", _PATRON_TYPED_INPUT_END_MAGIC, self._checksum
        )
        self._pending.extend(trailer)
        self._bytes += len(trailer)
        self._records += 1
        self._flush_pending()

    def publish(self) -> None:
        self._finish_payload()
        stream = self._stream
        self._stream = None
        stream.flush()
        stream.close()
        self._published = True

    def abort(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except (BrokenPipeError, OSError):
                pass

    @property
    def summary(self) -> Dict[str, Any]:
        return {
            "transport": "typed-binary-pipe",
            "bytes": self._bytes,
            "records": self._records,
            "crc32": self._checksum,
        }


class _NativeBinaryFileWriter(_NativeBinaryPipeWriter):
    """Atomically publish an explicit copy of the checked typed input stream."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        self._owned_backup: Optional[Path] = None
        self._recovery_backup: Optional[Path] = None
        super().__init__(None)
        self._owns_temporary = False
        self._stream = self._temporary.open("xb")
        self._owns_temporary = True

    def publish(self) -> None:
        if self._stream is None or self._published:
            raise ValidationError("native PATRON input writer is closed")
        self._finish_payload()
        stream = self._stream
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        replaced = False
        try:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            self._stream = None
            if self._path.exists():
                candidate = self._path.with_name(
                    f".{self._path.name}.{uuid.uuid4().hex}.backup"
                )
                os.link(self._path, candidate)
                # Ownership begins only after link(2) succeeds.  A collision
                # belongs to another writer and must never enter cleanup.
                self._owned_backup = candidate
            os.replace(self._temporary, self._path)
            replaced = True
            directory = os.open(self._path.parent, directory_flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self._published = True
        except BaseException:
            if replaced:
                try:
                    if self._owned_backup is None:
                        self._path.unlink(missing_ok=True)
                    else:
                        os.replace(self._owned_backup, self._path)
                        self._owned_backup = None
                    directory = os.open(self._path.parent, directory_flags)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError as rollback_error:
                    if self._owned_backup is not None:
                        self._recovery_backup = self._owned_backup
                        self._owned_backup = None
                        raise ValidationError(
                            "native PATRON input rollback failed; recovery "
                            f"backup retained at {self._recovery_backup}"
                        ) from rollback_error
                    raise
            raise
        finally:
            if self._published and self._owned_backup is not None:
                try:
                    self._owned_backup.unlink(missing_ok=True)
                    self._owned_backup = None
                    directory = os.open(self._path.parent, directory_flags)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError:
                    # The target has already been durably published.  A stale
                    # owner-unique backup is harmless and never a partial target.
                    pass

    def abort(self) -> None:
        try:
            if self._stream is not None:
                stream = self._stream
                self._stream = None
                stream.close()
        finally:
            if not self._published and self._owns_temporary:
                self._temporary.unlink(missing_ok=True)
            if self._owned_backup is not None and not self._published:
                self._owned_backup.unlink(missing_ok=True)
                self._owned_backup = None

    def __del__(self) -> None:
        try:
            self.abort()
        except BaseException:
            pass

    @property
    def summary(self) -> Dict[str, Any]:
        return {**super().summary, "transport": "atomic-binary-file"}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pressure_ir_source_digest(ir: Any) -> str:
    """Seal an in-memory logical IR or its exact compact source artifact."""

    from .partition_ir import CompactPartitionIR

    if not isinstance(ir, CompactPartitionIR):
        return _canonical_digest(ir.value)
    digest = hashlib.sha256()
    with ir.path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _flow_refinement_configuration(
    enabled: bool,
    cluster_count: int,
    *,
    version: int = 4,
    physical_feedback_sha256: Optional[str] = None,
    physical_feedback_scale: float = 0.0,
) -> Dict[str, Any]:
    if version not in (1, 2, 3, 4, 5, 8):
        raise ValidationError("native PATRON flow version is invalid")
    if (
        isinstance(physical_feedback_scale, bool)
        or not isinstance(physical_feedback_scale, (int, float))
        or not math.isfinite(float(physical_feedback_scale))
        or float(physical_feedback_scale) < 0.0
    ):
        raise ValidationError("native PATRON physical feedback scale is invalid")
    if version == 5 and (
        not enabled
        or not isinstance(physical_feedback_sha256, str)
        or len(physical_feedback_sha256) != 64
        or any(character not in "0123456789abcdef" for character in physical_feedback_sha256)
        or float(physical_feedback_scale) <= 0.0
    ):
        raise ValidationError("native PATRON physical feedback seal is invalid")
    if version != 5 and (
        physical_feedback_sha256 is not None
        or float(physical_feedback_scale) != 0.0
    ):
        raise ValidationError("legacy PATRON flow cannot consume physical feedback")
    result = {
        "enabled": enabled,
        "algorithm": (
            PATRON_FLOW_REFINEMENT_ALGORITHM_V8
            if version == 8
            else PATRON_FLOW_REFINEMENT_ALGORITHM_V5
            if version == 5
            else (
                PATRON_FLOW_REFINEMENT_ALGORITHM
                if version == 4
                else (
                    PATRON_FLOW_REFINEMENT_ALGORITHM_V3
                    if version == 3
                    else (
                        PATRON_FLOW_REFINEMENT_ALGORITHM_V2
                        if version == 2
                        else PATRON_FLOW_REFINEMENT_ALGORITHM_V1
                    )
                )
            )
        ),
        "maximum_corridor_clusters": cluster_count if enabled else 0,
        "corridor_distance": (
            PATRON_FLOW_CORRIDOR_DISTANCE if enabled else 0
        ),
        "piercing_strategy": (
            PATRON_FLOW_PIERCING_STRATEGY if enabled else 0
        ),
        "maximum_legal_candidates": (
            PATRON_FLOW_MAX_LEGAL_CANDIDATES if enabled else 0
        ),
        "maximum_polish_moves": (
            PATRON_FLOW_MAX_POLISH_MOVES if enabled else 0
        ),
    }
    if version >= 2:
        result.update(
            {
                "maximum_frontier_paths": (
                    PATRON_FLOW_MAX_FRONTIER_PATHS if enabled else 0
                ),
                "maximum_tail_moves": (
                    (
                        PATRON_FLOW_MAX_TAIL_MOVES
                        if version >= 3
                        else PATRON_FLOW_MAX_TAIL_MOVES_V2
                    )
                    if enabled
                    else 0
                ),
            }
        )
    if version >= 3:
        result["frontier_selection"] = "ranked-worst-path-window-v1"
    if version == 4:
        result.update(
            {
                "physical_hop_guard": "scale_ns*route_hops",
                "physical_hop_guard_scale_ns": (
                    PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS
                    if enabled
                    else 0.0
                ),
            }
        )
    if version == 5:
        result.update(
            {
                "physical_hop_guard": "disabled-by-endpoint-feedback",
                "physical_hop_guard_scale_ns": 0.0,
                "physical_feedback_schema": (
                    PARTITION_PHYSICAL_FEEDBACK_SCHEMA
                ),
                "physical_feedback_matching": (
                    "exact-current-endpoint-pair-v1"
                ),
                "physical_feedback_sha256": physical_feedback_sha256,
                "physical_feedback_scale": float(physical_feedback_scale),
            }
        )
    if version == 8:
        result.update(
            {
                "physical_hop_guard": "disabled",
                "physical_hop_guard_scale_ns": 0.0,
                "path_transition_objective": "timing-path-partition-transitions-v1",
                "selection_contract": "provider-neutral-predictive-timing-v1",
            }
        )
    return result


def _require_finite(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{context} must be finite")
    return float(value)


def _instance_to_cluster(
    clusters_artifact: Mapping[str, Any],
) -> Dict[str, str]:
    return {
        instance: cluster["id"]
        for cluster in clusters_artifact["clusters"]
        for instance in cluster["instances"]
    }


def _net_records(
    ir: EmuIR,
    clusters_artifact: Mapping[str, Any],
    cluster_by_instance: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    if cluster_by_instance is None:
        cluster_by_instance = _instance_to_cluster(clusters_artifact)
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    records = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        drivers = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sinks = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
        )
        if not drivers or not sinks:
            continue
        records.append(
            {
                "net": net["id"],
                "cut_class": net["cut_class"],
                "drivers": drivers,
                "sinks": sinks,
            }
        )
    return sorted(records, key=lambda record: record["net"])


def _compact_partition_pressure_graph(
    ir: Any,
    clusters_artifact: Mapping[str, Any],
    *,
    timing_database_path: Optional[Path] = None,
    timing_database: Optional[Mapping[str, Any]] = None,
) -> Tuple[
    _PackedStringOrdinalIndex,
    _PackedStringOrdinalIndex,
    _PackedStringOrdinalIndex,
    _PackedPressureNetSequence,
    _PackedPressureClusterSequence,
    Optional[_PackedPressurePathSequence],
]:
    """Build pressure facts by ordered joins over a compact EmuIR store.

    The temporary tables contain only cluster membership needed by this
    construction and are owned by SQLite's bounded temp store.  The returned
    graph retains names once in packed byte pools and endpoint membership as
    integer ordinals.  No decoded EmuIR net dictionaries, all-net Python set,
    or instance-to-cluster Python dictionary is created.
    """

    from .partition_ir import CompactPartitionIR

    if not isinstance(ir, CompactPartitionIR):
        raise TypeError("compact partition pressure graph requires compact IR")
    raw_clusters = clusters_artifact.get("clusters")
    if not isinstance(raw_clusters, list):
        raise ValidationError("partition pressure clusters are invalid")
    timing_payloads = (
        timing_database.get("payloads")
        if isinstance(timing_database, Mapping)
        else None
    )
    timing_descriptor = (
        timing_payloads.get("paths")
        if isinstance(timing_payloads, Mapping)
        else None
    )
    indexed_timing = (
        timing_database_path is not None
        and isinstance(timing_database, Mapping)
        and timing_database.get("schema") == STA_PATH_DATABASE_INDEX_SCHEMA
        and isinstance(timing_descriptor, Mapping)
        and timing_descriptor.get("format")
        == STA_PATH_DATABASE_SQLITE_FORMAT
    )
    connection = open_emuir_sqlite_read_only(ir.path)
    try:
        connection.execute("PRAGMA temp_store=FILE")
        connection.executescript(
            """
            CREATE TEMP TABLE pressure_clusters(
              cluster_ordinal INTEGER PRIMARY KEY,
              cluster_id TEXT NOT NULL UNIQUE
            );
            CREATE TEMP TABLE pressure_cluster_members(
              instance_id TEXT PRIMARY KEY,
              cluster_ordinal INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )
        cluster_records = _PackedPressureClusterSequence(raw_clusters)
        try:
            for ordinal, cluster in enumerate(raw_clusters):
                if not isinstance(cluster, Mapping):
                    raise ValidationError(
                        "partition pressure cluster record is invalid"
                    )
                cluster_id = cluster.get("id")
                members = cluster.get("instances")
                resources = cluster.get("resources")
                if (
                    not isinstance(cluster_id, str)
                    or not cluster_id
                    or not isinstance(members, list)
                    or not all(
                        isinstance(member, str) and member
                        for member in members
                    )
                    or not isinstance(resources, Mapping)
                    or "fixed_fpga" not in cluster
                ):
                    raise ValidationError(
                        "partition pressure cluster record is invalid"
                    )
                connection.execute(
                    "INSERT INTO pressure_clusters VALUES (?,?)",
                    (ordinal, cluster_id),
                )
                connection.executemany(
                    "INSERT INTO pressure_cluster_members VALUES (?,?)",
                    ((member, ordinal) for member in members),
                )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "partition pressure cluster identity or membership is duplicated"
            ) from error

        metadata = dict(
            connection.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('logical_schema','record_counts_json')"
            )
        )
        if (
            metadata.get("logical_schema") != EMUIR_SCHEMA
            or ir.value.get("schema") != EMUIR_SCHEMA
        ):
            raise ValidationError("compact EmuIR logical schema is invalid")
        try:
            counts = json.loads(metadata["record_counts_json"])
            if (
                not isinstance(counts, Mapping)
                or set(counts)
                != {"cell_records", "net_records", "endpoint_records"}
            ):
                raise TypeError("unexpected compact EmuIR record-count keys")
            cell_count = counts["cell_records"]
            net_count = counts["net_records"]
            endpoint_count = counts["endpoint_records"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "compact EmuIR record counts are invalid"
            ) from error
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in (cell_count, net_count, endpoint_count)
        ):
            raise ValidationError("compact EmuIR record counts are invalid")
        observed = connection.execute(
            "SELECT COUNT(*) FROM pressure_cluster_members"
        ).fetchone()[0]
        actual_cells = connection.execute(
            "SELECT COUNT(*) FROM cell_records"
        ).fetchone()[0]
        cell_bounds = connection.execute(
            "SELECT MIN(seq),MAX(seq) FROM cell_records"
        ).fetchone()
        dense_cells = (
            (cell_count == 0 and cell_bounds[0] is None and cell_bounds[1] is None)
            or (
                cell_count > 0
                and cell_bounds[0] == 0
                and cell_bounds[1] == cell_count - 1
            )
        )
        if (
            observed != cell_count
            or actual_cells != cell_count
            or not dense_cells
        ):
            raise ValidationError(
                "partition pressure cluster membership coverage disagrees"
            )
        missing = connection.execute(
            """SELECT c.id FROM cell_records c
                LEFT JOIN pressure_cluster_members m ON m.instance_id=c.id
               WHERE m.instance_id IS NULL LIMIT 1"""
        ).fetchone()
        extra = connection.execute(
            """SELECT m.instance_id FROM pressure_cluster_members m
                LEFT JOIN cell_records c ON c.id=m.instance_id
               WHERE c.id IS NULL LIMIT 1"""
        ).fetchone()
        if missing is not None or extra is not None:
            raise ValidationError(
                "partition pressure cluster membership coverage disagrees"
            )

        cluster_by_instance = _PackedStringOrdinalIndex()
        for row in connection.execute(
            "SELECT instance_id,cluster_ordinal "
            "FROM pressure_cluster_members ORDER BY instance_id"
        ):
            cluster_by_instance.append(
                row["instance_id"], row["cluster_ordinal"]
            )

        top_ports = set()
        for expected_port_seq, row in enumerate(
            connection.execute(
                "SELECT seq,record_json FROM port_records ORDER BY seq"
            )
        ):
            if row["seq"] != expected_port_seq:
                raise ValidationError("compact EmuIR port sequence is invalid")
            try:
                port = json.loads(row["record_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    "compact EmuIR port record is invalid"
                ) from error
            port_id = port.get("id") if isinstance(port, Mapping) else None
            if (
                not isinstance(port_id, str)
                or not port_id
                or port_id in top_ports
            ):
                raise ValidationError("compact EmuIR port record is invalid")
            top_ports.add(port_id)

        endpoint_rows = iter(
            connection.execute(
                """SELECT e.net_seq,e.role,e.ordinal,e.instance_seq,e.bit,
                          c.seq AS cell_owner_seq,
                          p.seq AS pin_name_seq,p.name AS port
                     FROM endpoint_records e
                     LEFT JOIN cell_records c ON c.seq=e.instance_seq
                     LEFT JOIN pin_names p ON p.seq=e.port_seq
                    ORDER BY e.net_seq,e.role,e.ordinal"""
            )
        )
        endpoint = next(endpoint_rows, None)
        observed_endpoints = 0
        known_nets = _PackedStringOrdinalIndex()
        observed_net_ordinals = 0
        for net in connection.execute(
            "SELECT seq,id,cut_class FROM net_records ORDER BY seq"
        ):
            net_seq = net["seq"]
            if net_seq != observed_net_ordinals:
                raise ValidationError("compact EmuIR net sequence is invalid")
            if not isinstance(net["id"], str) or not net["id"]:
                raise ValidationError("compact EmuIR net identity is invalid")
            if net["cut_class"] not in VALID_CUT_CLASSES:
                raise ValidationError("compact EmuIR net cut class is invalid")
            if endpoint is not None:
                endpoint_net_seq = endpoint["net_seq"]
                if (
                    isinstance(endpoint_net_seq, bool)
                    or not isinstance(endpoint_net_seq, int)
                    or endpoint_net_seq < 0
                    or endpoint_net_seq < net_seq
                ):
                    raise ValidationError("compact EmuIR has orphaned endpoints")
            endpoint_ordinals = [-1, -1]
            while endpoint is not None and endpoint["net_seq"] == net_seq:
                role = endpoint["role"]
                owner = endpoint["instance_seq"]
                if (
                    isinstance(role, bool)
                    or not isinstance(role, int)
                    or role not in (0, 1)
                ):
                    raise ValidationError("compact EmuIR endpoint role is invalid")
                ordinal = endpoint["ordinal"]
                if (
                    isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 0
                    or ordinal <= endpoint_ordinals[role]
                ):
                    raise ValidationError(
                        "compact EmuIR endpoint sequence is invalid"
                    )
                endpoint_ordinals[role] = ordinal
                if (
                    endpoint["pin_name_seq"] is None
                    or not isinstance(endpoint["port"], str)
                    or not endpoint["port"]
                ):
                    raise ValidationError("compact EmuIR endpoint port is invalid")
                bit = endpoint["bit"]
                if isinstance(bit, bool) or not isinstance(bit, int) or bit < 0:
                    raise ValidationError("compact EmuIR endpoint bit is invalid")
                if owner is not None:
                    if (
                        isinstance(owner, bool)
                        or not isinstance(owner, int)
                        or owner < 0
                        or owner >= cell_count
                        or endpoint["cell_owner_seq"] != owner
                    ):
                        raise ValidationError(
                            "compact EmuIR endpoint owner is invalid"
                        )
                elif endpoint["port"] not in top_ports:
                    raise ValidationError(
                        "compact EmuIR top-level endpoint is invalid"
                    )
                observed_endpoints += 1
                endpoint = next(endpoint_rows, None)
            observed_net_ordinals += 1
        if endpoint is not None:
            raise ValidationError("compact EmuIR has orphaned endpoints")
        if observed_net_ordinals != net_count:
            raise ValidationError("compact EmuIR net count disagrees")
        if observed_endpoints != endpoint_count:
            raise ValidationError("compact EmuIR record counts disagree")
        for row in connection.execute(
            "SELECT seq,id FROM net_records ORDER BY id"
        ):
            known_nets.append(row["id"], row["seq"])

        native_cluster_identities = _PackedStringOrdinalIndex()
        for native_ordinal, row in enumerate(
            connection.execute(
                "SELECT cluster_id,cluster_ordinal "
                "FROM pressure_clusters ORDER BY cluster_id"
            )
        ):
            native_cluster_identities.append(
                row["cluster_id"], row["cluster_ordinal"]
            )
            if native_ordinal >= 2**64:
                raise ValidationError(
                    "partition pressure cluster count is unsupported"
                )

        pressure_nets = _PackedPressureNetSequence(
            cluster_records, native_cluster_identities
        )
        transported = sorted(
            transported_cut_classes_for_clusters(clusters_artifact)
        )
        placeholders = ",".join("?" for _ in transported)
        if indexed_timing:
            connection.execute(
                "CREATE TEMP TABLE pressure_net_lookup("
                "net_id TEXT PRIMARY KEY,"
                "pressure_ordinal INTEGER NOT NULL UNIQUE,"
                "single_driver INTEGER NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                f"""INSERT INTO pressure_net_lookup
                SELECT net_id,
                       row_number() OVER (ORDER BY net_id)-1,
                       CASE WHEN driver_clusters=1 THEN 1 ELSE 0 END
                  FROM (
                    SELECT n.id AS net_id,
                           COUNT(DISTINCT CASE WHEN e.role=0
                                              THEN m.cluster_ordinal END)
                             AS driver_clusters,
                           COUNT(DISTINCT CASE WHEN e.role=1
                                              THEN m.cluster_ordinal END)
                             AS sink_clusters
                      FROM net_records n
                      JOIN endpoint_records e ON e.net_seq=n.seq
                      JOIN cell_records c ON c.seq=e.instance_seq
                      JOIN pressure_cluster_members m ON m.instance_id=c.id
                     WHERE n.cut_class IN ({placeholders})
                     GROUP BY n.id
                    HAVING COUNT(DISTINCT CASE WHEN e.role=0
                                               THEN m.cluster_ordinal END)>0
                       AND COUNT(DISTINCT CASE WHEN e.role=1
                                               THEN m.cluster_ordinal END)>0
                  ) ORDER BY net_id""",
                transported,
            )
            pressure_query = f"""SELECT n.id,n.cut_class,e.role,
                       m.cluster_ordinal,
                       pc.cluster_id,q.pressure_ordinal
                  FROM net_records n
                  JOIN endpoint_records e ON e.net_seq=n.seq
                  JOIN cell_records c ON c.seq=e.instance_seq
                  JOIN pressure_cluster_members m ON m.instance_id=c.id
                  JOIN pressure_clusters pc
                    ON pc.cluster_ordinal=m.cluster_ordinal
                  JOIN pressure_net_lookup q ON q.net_id=n.id
                 WHERE n.cut_class IN ({placeholders})
                 GROUP BY n.id,n.cut_class,e.role,m.cluster_ordinal,pc.cluster_id
                 ORDER BY q.pressure_ordinal,e.role,pc.cluster_id"""
        else:
            pressure_query = f"""SELECT n.id,n.cut_class,e.role,
                       m.cluster_ordinal,pc.cluster_id,
                       NULL AS pressure_ordinal
                  FROM net_records n
                  JOIN endpoint_records e ON e.net_seq=n.seq
                  JOIN cell_records c ON c.seq=e.instance_seq
                  JOIN pressure_cluster_members m ON m.instance_id=c.id
                  JOIN pressure_clusters pc
                    ON pc.cluster_ordinal=m.cluster_ordinal
                 WHERE n.cut_class IN ({placeholders})
                 GROUP BY n.id,n.cut_class,e.role,m.cluster_ordinal,pc.cluster_id
                 ORDER BY n.id,e.role,pc.cluster_id"""
        rows = connection.execute(pressure_query, transported)
        current_id: Optional[str] = None
        current_class: Optional[str] = None
        current_ordinal: Optional[int] = None
        drivers = array("I")
        sinks = array("I")

        def finish() -> None:
            if current_id is not None and drivers and sinks:
                assert current_class is not None
                if (
                    current_ordinal is not None
                    and current_ordinal != len(pressure_nets)
                ):
                    raise ValidationError(
                        "partition pressure net ordinal is invalid"
                    )
                pressure_nets.append_record(
                    current_id, current_class, drivers, sinks
                )

        for row in rows:
            if row["id"] != current_id:
                finish()
                current_id = row["id"]
                current_class = row["cut_class"]
                current_ordinal = (
                    None
                    if row["pressure_ordinal"] is None
                    else int(row["pressure_ordinal"])
                )
                drivers = array("I")
                sinks = array("I")
            if (
                row["cut_class"] != current_class
                or (
                    row["pressure_ordinal"] is not None
                    and int(row["pressure_ordinal"]) != current_ordinal
                )
                or row["role"] not in (0, 1)
            ):
                raise ValidationError(
                    "compact EmuIR pressure net relation is corrupt"
                )
            (drivers if row["role"] == 0 else sinks).append(
                int(row["cluster_ordinal"])
            )
        finish()
        pressure_nets.construction_identity_bytes = (
            known_nets.payload_bytes + cluster_by_instance.payload_bytes
        )
        indexed_paths: Optional[_PackedPressurePathSequence] = None
        if indexed_timing:
            assert timing_database_path is not None
            assert isinstance(timing_database, Mapping)
            assert isinstance(timing_descriptor, Mapping)
            descriptor = timing_descriptor
            payload_path = sta_path_database_payload_path(
                timing_database_path, timing_database
            )
            if payload_path is None:
                raise ValidationError(
                    "indexed timing database has no SQLite payload"
                )
            expected_paths = descriptor.get("records")
            if (
                isinstance(expected_paths, bool)
                or not isinstance(expected_paths, int)
                or expected_paths <= 0
            ):
                raise ValidationError(
                    "indexed timing database path count is invalid"
                )
            indexed_paths = _append_indexed_pressure_paths(
                connection,
                payload_path,
                expected_paths,
                known_nets,
                pressure_nets,
                cluster_records,
            )
        return (
            known_nets,
            cluster_by_instance,
            native_cluster_identities,
            pressure_nets,
            cluster_records,
            indexed_paths,
        )
    finally:
        connection.close()


def _append_indexed_pressure_paths(
    connection: sqlite3.Connection,
    payload_path: Path,
    expected_paths: int,
    known_nets: _PackedStringOrdinalIndex,
    pressure_nets: _PackedPressureNetSequence,
    clusters: _PackedPressureClusterSequence,
) -> _PackedPressurePathSequence:
    """Project TimingPathDB rows onto packed pressure ordinals in SQL.

    The former production path decoded every TimingPathDB row into a Python
    dictionary, then performed two UTF-8 binary searches for every path/net
    incidence.  NVDLA has millions of incidences, so that adapter consumed
    more than an hour of one CPU even after the retained representation was
    packed.  Keep the authoritative stores immutable, attach the indexed
    timing payload read-only, and let SQLite join identities to the EmuIR and
    pressure-net ordinal domains once.
    """

    alias = "pressure_timing"
    uri = immutable_sqlite_uri(payload_path)
    try:
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
        version = connection.execute(
            f"PRAGMA {alias}.user_version"
        ).fetchone()[0]
        if version != 1:
            raise ValidationError(
                "indexed timing database SQLite version is invalid"
            )
        paths = _PackedPressurePathSequence(
            known_nets, pressure_nets, clusters
        )
        path_cursor = connection.execute(
            f"""SELECT p.ordinal,p.path_id,p.clock_domain,
                       p.clock_period_ns,p.slack_ns,
                       launch.cluster_ordinal,capture.cluster_ordinal
                  FROM {alias}.paths p
                  LEFT JOIN pressure_cluster_members launch
                    ON launch.instance_id=p.start_instance
                  LEFT JOIN pressure_cluster_members capture
                    ON capture.instance_id=p.end_instance
                 ORDER BY p.ordinal"""
        )
        net_cursor = iter(connection.execute(
            f"""SELECT pn.path_ordinal,pn.net_ordinal,n.seq,
                       q.pressure_ordinal,q.single_driver,pn.net_id
                  FROM {alias}.path_nets pn
                  LEFT JOIN net_records n ON n.id=pn.net_id
                  LEFT JOIN pressure_net_lookup q ON q.net_id=pn.net_id
                 ORDER BY pn.path_ordinal,pn.net_ordinal"""
        ))
        net_row = next(net_cursor, None)
        observed_paths = 0
        for row in path_cursor:
            ordinal = int(row[0])
            if ordinal != observed_paths:
                raise ValidationError(
                    "indexed timing database path order is invalid"
                )
            identity = row[1]
            period = _require_finite(
                row[3], f"path {identity!r} period"
            )
            slack = _require_finite(
                row[4], f"path {identity!r} slack"
            )
            if not isinstance(identity, str) or not identity or period <= 0.0:
                raise ValidationError(
                    "indexed timing database path record is invalid"
                )
            source_net_ordinals = array("Q")
            pressure_net_ordinals = array("I")
            expected_net_ordinal = 0
            single_driver_pressure_nets = True
            while net_row is not None and int(net_row[0]) == ordinal:
                if int(net_row[1]) != expected_net_ordinal:
                    raise ValidationError(
                        "indexed timing database net order is invalid"
                    )
                if net_row[2] is None:
                    raise ValidationError(
                        f"partition pressure path {identity!r} references "
                        f"unknown net {net_row[5]!r}"
                    )
                source_net_ordinals.append(int(net_row[2]))
                if net_row[3] is not None:
                    pressure_net_ordinals.append(int(net_row[3]))
                    single_driver_pressure_nets = (
                        single_driver_pressure_nets and bool(net_row[4])
                    )
                expected_net_ordinal += 1
                net_row = next(net_cursor, None)
            if expected_net_ordinal == 0:
                raise ValidationError(
                    "indexed timing database path has no nets"
                )
            start_cluster = None if row[5] is None else int(row[5])
            end_cluster = None if row[6] is None else int(row[6])
            endpoint_exact = (
                start_cluster is not None
                and end_cluster is not None
                and single_driver_pressure_nets
            )
            paths.append_ordinals(
                identity,
                row[2],
                period,
                slack,
                source_net_ordinals,
                pressure_net_ordinals,
                start_cluster,
                end_cluster,
                endpoint_exact,
            )
            observed_paths += 1
        if net_row is not None:
            raise ValidationError(
                "indexed timing database contains orphan path nets"
            )
        if observed_paths != expected_paths:
            raise ValidationError(
                "indexed timing database path count disagrees"
            )
        paths.indexed_sql_projection = True
        return paths
    except sqlite3.DatabaseError as error:
        raise ValidationError(
            "indexed timing database SQLite payload is invalid"
        ) from error
    finally:
        try:
            connection.execute(f"DETACH DATABASE {alias}")
        except sqlite3.DatabaseError:
            pass


def _shortest_routes(
    platform: Platform, route_constraints: Mapping[str, Any]
) -> Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]] = {}
    for source in sorted(adjacency):
        # The tuple deliberately includes the complete arc identity.  Equal
        # delay/hop alternatives therefore have a stable provider-independent
        # ordering rather than depending on heap insertion order.
        best: Dict[str, Tuple[float, int, Tuple[Tuple[str, str, str], ...]]] = {
            source: (0.0, 0, ())
        }
        queue: List[
            Tuple[float, int, Tuple[Tuple[str, str, str], ...], str]
        ] = [(0.0, 0, (), source)]
        while queue:
            delay, hops, identity, node = heapq.heappop(queue)
            if best.get(node) != (delay, hops, identity):
                continue
            for arc in adjacency[node]:
                arc_identity = (arc["link"], arc["from"], arc["to"])
                candidate = (
                    delay
                    + route_link_delay_ns(
                        platform,
                        arc["link"],
                        arc["from"],
                        arc["to"],
                        route_constraints,
                    ),
                    hops + 1,
                    identity + (arc_identity,),
                )
                if arc["to"] not in best or candidate < best[arc["to"]]:
                    best[arc["to"]] = candidate
                    heapq.heappush(queue, (*candidate, arc["to"]))
        by_sink: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        for sink in sorted(adjacency):
            if sink not in best:
                by_sink[sink] = None
                continue
            by_sink[sink] = [
                {
                    "link": link,
                    "from": left,
                    "to": right,
                }
                for link, left, right in best[sink][2]
            ]
        result[source] = by_sink
    return result


def _reconstruct_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    *,
    include_source_seals: bool = True,
    timing_database_path: Optional[Path] = None,
) -> Dict[str, Any]:

    if timing_database.get("design") != ir.value["design"]["name"]:
        raise ValidationError("partition pressure timing design mismatch")
    normalization = _validate_database_normalization(
        timing_database.get("normalization")
    )
    path_payloads = timing_database.get("payloads")
    path_descriptor = (
        path_payloads.get("paths")
        if isinstance(path_payloads, Mapping)
        else None
    )
    indexed_unique_path_ids = (
        timing_database_path is not None
        and timing_database.get("schema") == STA_PATH_DATABASE_INDEX_SCHEMA
        and isinstance(path_descriptor, Mapping)
        and path_descriptor.get("format") == STA_PATH_DATABASE_SQLITE_FORMAT
    )
    from .partition_ir import CompactPartitionIR

    if isinstance(ir, CompactPartitionIR):
        (
            known_nets,
            cluster_by_instance,
            _native_cluster_identities,
            compact_pressure_nets,
            clusters,
            indexed_paths,
        ) = _compact_partition_pressure_graph(
            ir,
            clusters_artifact,
            timing_database_path=timing_database_path,
            timing_database=timing_database,
        )
        pressure_nets: Any = compact_pressure_nets
        pressure_net_lookup: Any = compact_pressure_nets
        paths: Any = (
            indexed_paths
            if indexed_paths is not None
            else _PackedPressurePathSequence(
                known_nets,
                compact_pressure_nets,
                clusters,
            )
        )
    else:
        known_nets = {net["id"] for net in ir.value["nets"]}
        cluster_by_instance = _instance_to_cluster(clusters_artifact)
        pressure_nets = {
            record["net"]: record
            for record in _net_records(
                ir, clusters_artifact, cluster_by_instance
            )
        }
        pressure_net_lookup = pressure_nets
        clusters = [
            {
                "cluster": cluster["id"],
                "cells": len(cluster["instances"]),
                "resources": dict(sorted(cluster["resources"].items())),
                "fixed_fpga": cluster["fixed_fpga"],
            }
            for cluster in clusters_artifact["clusters"]
        ]
        paths = []
    # TimingPathDB v3 owns a UNIQUE path_id column.  Retain the explicit set
    # only for legacy/inline inputs that lack that typed uniqueness contract.
    seen_paths: Optional[set[str]] = (
        None if indexed_unique_path_ids else set()
    )
    timing_records = (
        ()
        if isinstance(ir, CompactPartitionIR) and indexed_paths is not None
        else (
            iter_sta_path_database_paths(
                timing_database_path,
                timing_database,
                verify_payload=False,
            )
            if timing_database_path is not None
            else timing_database.get("paths", [])
        )
    )
    for index, raw in enumerate(timing_records):
        if not isinstance(raw, dict):
            raise ValidationError(
                f"partition pressure timing path {index} is invalid"
            )
        path_id = raw.get("id")
        path_nets = raw.get("path_nets")
        if (
            not isinstance(path_id, str)
            or not path_id
            or (seen_paths is not None and path_id in seen_paths)
            or not isinstance(path_nets, list)
            or not all(isinstance(net, str) for net in path_nets)
            or not path_nets
        ):
            raise ValidationError(
                f"partition pressure timing path {index} is invalid"
            )
        source_net_ordinals: Optional[array] = None
        pressure_net_ordinals: Optional[array] = None
        if isinstance(paths, _PackedPressurePathSequence):
            source_net_ordinals = array("Q")
            pressure_net_ordinals = array("I")
            unknown = []
            single_driver_pressure_nets = True
            for net in path_nets:
                source_ordinal = known_nets.get(net)
                if source_ordinal is None:
                    unknown.append(net)
                    continue
                source_net_ordinals.append(source_ordinal)
                pressure_ordinal = compact_pressure_nets.ordinal(net)
                if pressure_ordinal is not None:
                    pressure_net_ordinals.append(pressure_ordinal)
                    drivers, _sinks = compact_pressure_nets.native_net_members(
                        pressure_ordinal
                    )
                    if len(drivers) != 1:
                        single_driver_pressure_nets = False
            unknown = sorted(set(unknown))
        else:
            unknown = sorted({net for net in path_nets if net not in known_nets})
        if unknown:
            raise ValidationError(
                f"partition pressure path {path_id!r} references unknown nets "
                f"{unknown[:8]}"
            )
        period = _require_finite(
            raw.get("clock_period_ns"), f"path {path_id!r} period"
        )
        slack = _require_finite(
            raw.get("slack_ns"), f"path {path_id!r} slack"
        )
        if period <= 0.0:
            raise ValidationError(
                f"path {path_id!r} clock period must be positive"
            )
        if seen_paths is not None:
            seen_paths.add(path_id)
        startpoint = raw.get("startpoint")
        endpoint = raw.get("endpoint")
        start_instance = (
            startpoint.get("instance")
            if isinstance(startpoint, dict)
            else None
        )
        end_instance = (
            endpoint.get("instance")
            if isinstance(endpoint, dict)
            else None
        )
        start_cluster_ordinal = cluster_by_instance.get(start_instance)
        end_cluster_ordinal = cluster_by_instance.get(end_instance)
        if isinstance(cluster_by_instance, _PackedStringOrdinalIndex):
            start_cluster = (
                None
                if start_cluster_ordinal is None
                else clusters[start_cluster_ordinal]["cluster"]
            )
            end_cluster = (
                None
                if end_cluster_ordinal is None
                else clusters[end_cluster_ordinal]["cluster"]
            )
        else:
            start_cluster = start_cluster_ordinal
            end_cluster = end_cluster_ordinal
        if isinstance(paths, _PackedPressurePathSequence):
            endpoint_exact = (
                start_cluster is not None
                and end_cluster is not None
                and single_driver_pressure_nets
            )
        else:
            considered_records = []
            for net in path_nets:
                pressure_record = pressure_net_lookup.get(net)
                if pressure_record is not None:
                    considered_records.append(pressure_record)
            endpoint_exact = (
                start_cluster is not None
                and end_cluster is not None
                and all(
                    len(record["drivers"]) == 1
                    for record in considered_records
                )
            )
        if isinstance(paths, _PackedPressurePathSequence):
            assert source_net_ordinals is not None
            assert pressure_net_ordinals is not None
            paths.append_ordinals(
                path_id,
                raw.get("clock_domain"),
                period,
                slack,
                source_net_ordinals,
                pressure_net_ordinals,
                start_cluster_ordinal,
                end_cluster_ordinal,
                endpoint_exact,
            )
        else:
            record = {
                "path": path_id,
                "clock_domain": raw.get("clock_domain"),
                "clock_period_ns": period,
                "base_slack_ns": slack,
                "path_nets": list(path_nets),
                "transition_model": (
                    "endpoint-exact-reverse-chain-v1"
                    if endpoint_exact
                    else "conservative-net-worst-v1"
                ),
            }
            if endpoint_exact:
                record["start_cluster"] = start_cluster
                record["end_cluster"] = end_cluster
            paths.append(record)
    if not paths:
        raise ValidationError("partition pressure requires timing paths")

    adjacency, _, capacities = build_directed_graph(
        platform,
        route_constraints,
        reserve_arrival_and_commit=(
            clusters_artifact.get("policy", {}).get("cut_mode")
            == CUT_MODE_STATIC_EXACT
        ),
    )
    if not any(adjacency.values()):
        raise ValidationError("partition pressure requires routable links")
    links = {link.id: link for link in platform.links}
    capacity_records = []
    for key, record in sorted(capacities.items()):
        link = links[record["link"]]
        capacity_records.append(
            {
                **record,
                "lanes": link.transport_bits_per_cycle_per_direction,
                "fabric_clock_mhz": link.fabric_clock_mhz,
            }
        )
    model = {
        "schema": PARTITION_PRESSURE_MODEL_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": PARTITION_PRESSURE_PROVIDER,
        "configuration": {
            "gain_quantum": GAIN_QUANTUM,
            "boundary_fanout_penalty": "scale*log2(1+remote_sink_clusters)",
            "boundary_fanout_penalty_scale_ns": (
                BOUNDARY_FANOUT_PENALTY_SCALE_NS
            ),
            "max_scalable_sweeps": MAX_SCALABLE_SWEEPS,
            "scalable_ejection_critical_limit": (
                SCALABLE_EJECTION_CRITICAL_LIMIT
            ),
            "scalable_ejection_donor_limit": SCALABLE_EJECTION_DONOR_LIMIT,
            "max_scalable_ejections": MAX_SCALABLE_EJECTIONS,
            "max_route_hops": route_constraints.get("max_route_hops"),
            "frame_slots": route_constraints["frame_slots"],
            "tdm_max_ratio": route_constraints["tdm_max_ratio"],
            "tdm_ratio_quantum": route_constraints["tdm_ratio_quantum"],
            "predicted_wait": "sum((domain_ratio-1)*ratio_delay_slope_ns)",
            "path_delay": (
                "endpoint_exact_reverse_chain_else_"
                "sum(max_remote_sink_delay_per_path_net)"
            ),
        },
        "normalization": normalization,
        "clusters": clusters,
        "fpgas": [
            {
                "fpga": fpga.id,
                "effective_capacity": dict(
                    sorted(fpga.effective_capacity.items())
                ),
            }
            for fpga in active_partition_fpgas(platform, constraints)
        ],
        "capacities": capacity_records,
        "shortest_routes": _shortest_routes(platform, route_constraints),
        "nets": (
            pressure_nets
            if isinstance(pressure_nets, _PackedPressureNetSequence)
            else list(pressure_nets.values())
        ),
        "paths": paths,
    }
    if include_source_seals:
        model["sources"] = {
            "ir_sha256": _pressure_ir_source_digest(ir),
            "clusters_sha256": _canonical_digest(clusters_artifact),
            "constraints_sha256": _canonical_digest(constraints),
            "timing_database_sha256": _canonical_digest(timing_database),
            "route_constraints_sha256": _canonical_digest(
                route_constraints
            ),
        }
    return model


def build_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    *,
    independent_validation: bool = True,
    timing_database_path: Optional[Path] = None,
    native_store_model: bool = False,
) -> Dict[str, Any]:
    """Select a bounded native store request or the explicit reference model."""

    if not isinstance(native_store_model, bool):
        raise ValidationError("native pressure model selection must be boolean")
    if native_store_model:
        from .phase3_pressure_native import NativePressureModel, supports_native_pressure_model
        if independent_validation or timing_database_path is None or not supports_native_pressure_model(ir, clusters_artifact, timing_database):
            raise ValidationError("native pressure model requires compact indexed production inputs")
        return NativePressureModel(ir, platform, clusters_artifact, constraints, timing_database, route_constraints, timing_database_path)
    model = _reconstruct_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
        include_source_seals=independent_validation,
        timing_database_path=timing_database_path,
    )
    if independent_validation:
        validate_partition_pressure_model(
            ir,
            platform,
            clusters_artifact,
            constraints,
            timing_database,
            route_constraints,
            model,
            timing_database_path=timing_database_path,
        )
    return model


def validate_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    *,
    timing_database_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if model.get("schema") != PARTITION_PRESSURE_MODEL_SCHEMA:
        raise ValidationError("partition pressure model schema is invalid")
    # Rebuilding from primary inputs is the independent source-binding gate.
    expected = _reconstruct_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
        timing_database_path=timing_database_path,
    )
    if model != expected:
        raise ValidationError("partition pressure model reconstruction mismatch")
    expected_sources = {
        "ir_sha256": _pressure_ir_source_digest(ir),
        "clusters_sha256": _canonical_digest(clusters_artifact),
        "constraints_sha256": _canonical_digest(constraints),
        "timing_database_sha256": _canonical_digest(timing_database),
        "route_constraints_sha256": _canonical_digest(route_constraints),
    }
    if model.get("sources") != expected_sources:
        raise ValidationError("partition pressure model source seal mismatch")
    if model.get("design") != ir.value["design"]["name"]:
        raise ValidationError("partition pressure model design mismatch")
    if model.get("platform") != platform.name:
        raise ValidationError("partition pressure model platform mismatch")
    if model.get("provider") != PARTITION_PRESSURE_PROVIDER:
        raise ValidationError("partition pressure model provider mismatch")
    return {
        "status": "pass",
        "clusters": len(model.get("clusters", [])),
        "nets": len(model.get("nets", [])),
        "paths": len(model.get("paths", [])),
        "capacity_domains": len(model.get("capacities", [])),
    }


def _capacity_key_by_arc(model: Mapping[str, Any]) -> Dict[Tuple[str, str, str], str]:
    by_link = defaultdict(list)
    for record in model["capacities"]:
        by_link[record["link"]].append(record)
    result = {}
    for source, by_sink in model["shortest_routes"].items():
        for sink, route in by_sink.items():
            if route is None:
                continue
            for arc in route:
                candidates = by_link[arc["link"]]
                shared = next(
                    (item for item in candidates if item["direction"] == "shared"),
                    None,
                )
                record = shared or next(
                    item
                    for item in candidates
                    if item["direction"] == f"{arc['from']}->{arc['to']}"
                )
                result[(arc["link"], arc["from"], arc["to"])] = record["key"]
    return result


def _predicted_route_delay(
    platform: Platform,
    route_constraints: Mapping[str, Any],
    domain_by_arc: Mapping[Tuple[str, str, str], str],
    domain_ratios: Mapping[str, int],
    route: Iterable[Mapping[str, Any]],
    *,
    include_tdm_wait: bool,
    physical_hop_guard_scale_ns: float = 0.0,
) -> float:
    delay = 0.0
    for arc in route:
        domain = domain_by_arc[(arc["link"], arc["from"], arc["to"])]
        if include_tdm_wait:
            delay += route_hop_delay_ns(
                platform, arc["link"], arc["from"], arc["to"],
                route_constraints, domain_ratios[domain],
            )
        else:
            delay += route_link_delay_ns(
                platform, arc["link"], arc["from"], arc["to"],
                route_constraints,
            )
        delay += physical_hop_guard_scale_ns
    return delay


def _boundary_fanout_penalty(
    model: Mapping[str, Any], remote_sink_clusters: int
) -> float:
    if remote_sink_clusters <= 0:
        return 0.0
    scale = _require_finite(
        model["configuration"]["boundary_fanout_penalty_scale_ns"],
        "partition pressure boundary fanout penalty scale",
    )
    if scale < 0.0:
        raise ValidationError(
            "partition pressure boundary fanout penalty scale must be "
            "non-negative"
        )
    return scale * math.log2(1.0 + remote_sink_clusters)


def _endpoint_exact_path_transport(
    path: Mapping[str, Any],
    net_by_id: Mapping[str, Mapping[str, Any]],
    cluster_parts: Mapping[str, str],
    model: Mapping[str, Any],
    platform: Platform,
    route_constraints: Mapping[str, Any],
    domain_by_arc: Mapping[Tuple[str, str, str], str],
    domain_ratios: Mapping[str, int],
    *,
    include_tdm_wait: bool,
    physical_hop_guard_scale_ns: float = 0.0,
) -> Tuple[float, List[str], int, int, int]:
    """Recover the concrete fanout branch used by one timing path."""

    target = cluster_parts[path["end_cluster"]]
    reverse_transitions: List[Tuple[str, str]] = []
    transport = 0.0
    path_hops = 0
    path_ratio_sum = 0
    max_hops = model["configuration"]["max_route_hops"]
    for net_id in reversed(path["path_nets"]):
        net = net_by_id.get(net_id)
        if net is None:
            continue
        sources = {cluster_parts[cluster] for cluster in net["drivers"]}
        if len(sources) != 1:
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} has a multi-driver "
                f"pressure net {net_id!r}"
            )
        source = next(iter(sources))
        sink_parts = {cluster_parts[cluster] for cluster in net["sinks"]}
        if target not in sink_parts:
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} cannot reach "
                f"partition {target!r} through net {net_id!r}"
            )
        if source == target:
            continue
        remote_sink_clusters = sum(
            cluster_parts[sink] == target for sink in net["sinks"]
        )
        route = model["shortest_routes"][source][target]
        if route is None or (
            max_hops is not None and len(route) > max_hops
        ):
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} has an illegal "
                f"transition {source!r}->{target!r}"
            )
        transport += _predicted_route_delay(
            platform,
            route_constraints,
            domain_by_arc,
            domain_ratios,
            route,
            include_tdm_wait=include_tdm_wait,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        )
        transport += _boundary_fanout_penalty(
            model, remote_sink_clusters
        )
        path_hops += len(route)
        path_ratio_sum += max(
            domain_ratios[
                domain_by_arc[(arc["link"], arc["from"], arc["to"])]
            ]
            for arc in route
        )
        reverse_transitions.append((source, target))
        target = source
    if cluster_parts[path["start_cluster"]] != target:
        raise ValidationError(
            f"endpoint-exact path {path['path']!r} launch/capture chain "
            "is inconsistent"
        )
    partition_sequence: List[str] = []
    for source, sink in reversed(reverse_transitions):
        for part in (source, sink):
            if not partition_sequence or partition_sequence[-1] != part:
                partition_sequence.append(part)
    return (
        transport,
        partition_sequence,
        len(reverse_transitions),
        path_hops,
        path_ratio_sum,
    )


def evaluate_partition_pressure(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    cluster_assignment: Mapping[str, str],
    *,
    include_tdm_wait: bool = True,
    physical_hop_guard_scale_ns: float = 0.0,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    path_transition_objective: bool = False,
) -> Dict[str, Any]:
    """Fully recompute the PATRON objective for one compact assignment."""

    if not isinstance(path_transition_objective, bool):
        raise ValidationError(
            "partition pressure transition-objective flag is invalid"
        )

    assignment = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=PARTITION_PRESSURE_PROVIDER,
        seed=0,
    )
    validate_partition_artifacts(ir, platform, clusters_artifact, assignment)
    physical_hop_guard_scale_ns = _require_finite(
        physical_hop_guard_scale_ns,
        "partition pressure physical hop guard scale",
    )
    if physical_hop_guard_scale_ns < 0.0:
        raise ValidationError(
            "partition pressure physical hop guard scale must be non-negative"
        )
    physical_feedback_scale = _require_finite(
        physical_feedback_scale,
        "partition pressure physical feedback scale",
    )
    if physical_feedback_scale < 0.0:
        raise ValidationError(
            "partition pressure physical feedback scale must be non-negative"
        )
    if physical_feedback is None:
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "partition pressure physical feedback scale has no artifact"
            )
        feedback_by_path: Dict[str, Mapping[str, Any]] = {}
    else:
        if (
            physical_feedback.get("schema")
            != PARTITION_PHYSICAL_FEEDBACK_SCHEMA
            or physical_feedback_scale <= 0.0
            or not isinstance(physical_feedback.get("paths"), list)
        ):
            raise ValidationError(
                "partition pressure physical feedback configuration is invalid"
            )
        feedback_by_path = {
            record["path"]: record for record in physical_feedback["paths"]
        }
        if len(feedback_by_path) != len(physical_feedback["paths"]):
            raise ValidationError(
                "partition pressure physical feedback paths are duplicated"
            )
    cluster_parts = dict(cluster_assignment)
    domain_by_arc = _capacity_key_by_arc(model)
    capacity_by_key = {record["key"]: record for record in model["capacities"]}
    domain_loads = {key: 0 for key in capacity_by_key}
    routes_by_net: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    max_hops = model["configuration"]["max_route_hops"]
    unreachable = []

    for net in model["nets"]:
        sources = sorted({cluster_parts[item] for item in net["drivers"]})
        sink_part_counts: Dict[str, int] = defaultdict(int)
        for cluster in net["sinks"]:
            sink_part_counts[cluster_parts[cluster]] += 1
        sinks = sorted(sink_part_counts)
        for source in sources:
            for sink in sinks:
                if sink == source:
                    continue
                route = model["shortest_routes"][source][sink]
                if route is None or (
                    max_hops is not None and len(route) > max_hops
                ):
                    unreachable.append(
                        {"net": net["net"], "from": source, "to": sink}
                    )
                    continue
                route_record = {
                    "from": source,
                    "to": sink,
                    "arcs": route,
                    "remote_sink_clusters": sink_part_counts[sink],
                }
                routes_by_net[net["net"]].append(route_record)
                for arc in route:
                    domain_loads[
                        domain_by_arc[(arc["link"], arc["from"], arc["to"])]
                    ] += 1
    if unreachable:
        raise ValidationError(
            "partition pressure assignment violates route reachability/hops: "
            f"{unreachable[:8]}"
        )

    domain_ratios = {
        key: estimate_tdm_ratio(
            load,
            capacity_by_key[key]["lanes"],
            route_constraints,
            is_sll=(
                capacity_by_key[key]["link"]
                in set(route_constraints["sll_links"])
            ),
        )
        for key, load in domain_loads.items()
    }
    domain_ratios = apply_bank_domain_ratios(
        capacity_by_key, domain_loads, domain_ratios, route_constraints
    )
    transport_capacity_violations = 0
    transport_capacity_excess_bits = 0
    for key, capacity in capacity_by_key.items():
        excess = max(0, domain_loads[key] - int(capacity["capacity_bits"]))
        if excess:
            transport_capacity_violations += 1
            transport_capacity_excess_bits += excess
        opposite = capacity.get("opposite_capacity_key")
        if (
            capacity.get("bank_group")
            and isinstance(opposite, str)
            and key < opposite
        ):
            shared_capacity = (
                int(capacity["bank_count"])
                * int(capacity["wires_per_bank"])
                * int(route_constraints["frame_slots"])
            )
            shared_excess = max(
                0,
                domain_loads[key]
                + domain_loads[opposite]
                - shared_capacity,
            )
            if shared_excess:
                transport_capacity_violations += 1
                transport_capacity_excess_bits += shared_excess
    net_delay: Dict[str, float] = {}
    net_worst_transition: Dict[str, Tuple[str, str]] = {}
    total_bit_hops = 0
    for net, routes in routes_by_net.items():
        maximum = 0.0
        worst_transition: Optional[Tuple[str, str]] = None
        for route in routes:
            delay = _predicted_route_delay(
                platform,
                route_constraints,
                domain_by_arc,
                domain_ratios,
                route["arcs"],
                include_tdm_wait=include_tdm_wait,
                physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            )
            delay += _boundary_fanout_penalty(
                model, route["remote_sink_clusters"]
            )
            transition = (route["from"], route["to"])
            if delay > maximum or (
                abs(delay - maximum) <= 1.0e-12
                and (
                    worst_transition is None
                    or transition < worst_transition
                )
            ):
                maximum = delay
                worst_transition = transition
            total_bit_hops += len(route["arcs"])
        net_delay[net] = maximum
        if worst_transition is not None:
            net_worst_transition[net] = worst_transition

    normalization = model["normalization"]
    net_by_id = {net["net"]: net for net in model["nets"]}
    path_records = []
    for path in model["paths"]:
        path_hops = 0
        path_ratio_sum = 0
        if path["transition_model"] == "endpoint-exact-reverse-chain-v1":
            (
                transport,
                partition_sequence,
                transitions,
                path_hops,
                path_ratio_sum,
            ) = (
                _endpoint_exact_path_transport(
                    path,
                    net_by_id,
                    cluster_parts,
                    model,
                    platform,
                    route_constraints,
                    domain_by_arc,
                    domain_ratios,
                    include_tdm_wait=include_tdm_wait,
                    physical_hop_guard_scale_ns=(
                        physical_hop_guard_scale_ns
                    ),
                )
            )
        else:
            transport = sum(
                net_delay.get(net, 0.0) for net in path["path_nets"]
            )
            partition_sequence = []
            for net in path["path_nets"]:
                transition = net_worst_transition.get(net)
                if transition is None:
                    continue
                route = model["shortest_routes"][transition[0]][transition[1]]
                if route is None:
                    raise ValidationError(
                        "partition pressure path has an unreachable worst "
                        f"transition {transition!r}"
                    )
                path_hops += len(route)
                path_ratio_sum += max(
                    domain_ratios[
                        domain_by_arc[
                            (arc["link"], arc["from"], arc["to"])
                        ]
                    ]
                    for arc in route
                )
                for part in transition:
                    if (
                        not partition_sequence
                        or partition_sequence[-1] != part
                    ):
                        partition_sequence.append(part)
            transitions = max(0, len(partition_sequence) - 1)
        feedback_record = feedback_by_path.get(path["path"])
        feedback_delay = 0.0
        if (
            feedback_record is not None
            and len(partition_sequence) > 1
            and partition_sequence[0]
            == feedback_record["observed_source_fpga"]
            and partition_sequence[-1]
            == feedback_record["observed_sink_fpga"]
        ):
            feedback_delay = (
                _require_finite(
                    feedback_record["positive_residual_ns"],
                    "partition pressure physical feedback residual",
                )
                * physical_feedback_scale
            )
            transport += feedback_delay
        predicted_slack = path["base_slack_ns"] - transport
        normalized = _normalized_slack(
            path["clock_period_ns"], predicted_slack, normalization
        )
        seen_parts = set()
        snaking = 0
        for part in partition_sequence:
            if part in seen_parts:
                snaking += 1
            seen_parts.add(part)
        path_records.append(
            {
                "path": path["path"],
                "transition_model": path["transition_model"],
                "transport_delay_ns": transport,
                "physical_feedback_delay_ns": feedback_delay,
                "predicted_slack_ns": predicted_slack,
                "normalized_slack": normalized,
                "partition_transitions": transitions,
                "physical_hops": path_hops,
                "tdm_ratio_sum": path_ratio_sum,
                "snaking": snaking,
                "partition_sequence": partition_sequence,
            }
        )
    worst = min(record["normalized_slack"] for record in path_records)
    total_negative = sum(
        min(0.0, record["normalized_slack"]) for record in path_records
    )
    maximum_ratio = max(domain_ratios.values(), default=1)
    total_snaking = sum(record["snaking"] for record in path_records)
    total_transitions = sum(
        record["partition_transitions"] for record in path_records
    )
    maximum_path_hops = max(
        (record["physical_hops"] for record in path_records), default=0
    )
    maximum_path_tdm_ratio_sum = max(
        (record["tdm_ratio_sum"] for record in path_records), default=0
    )
    cut_bits = sum(len(routes) for routes in routes_by_net.values())
    metrics = {
        "worst_normalized_slack": worst,
        "total_negative_normalized_slack": total_negative,
        "negative_paths": sum(
            record["normalized_slack"] < 0.0 for record in path_records
        ),
        "maximum_predicted_tdm_ratio": maximum_ratio,
        "maximum_capacity_domain_load": max(domain_loads.values(), default=0),
        "total_path_snaking": total_snaking,
        "total_path_partition_transitions": total_transitions,
        "maximum_path_hops": maximum_path_hops,
        "maximum_path_tdm_ratio_sum": maximum_path_tdm_ratio_sum,
        "total_bit_hops": total_bit_hops,
        "cut_bits": cut_bits,
        "transport_capacity_violations": transport_capacity_violations,
        "transport_capacity_excess_bits": transport_capacity_excess_bits,
    }
    metrics["objective_key"] = [
        -worst,
        -total_negative,
        metrics["negative_paths"],
        maximum_ratio,
        metrics["maximum_capacity_domain_load"],
        total_transitions if path_transition_objective else total_snaking,
        total_bit_hops,
        cut_bits,
    ]
    return {
        "assignment": assignment,
        "metrics": metrics,
        "paths": path_records,
        "capacity_domains": [
            {
                "capacity_domain": key,
                "load": domain_loads[key],
                "predicted_tdm_ratio": domain_ratios[key],
            }
            for key in sorted(domain_loads)
        ],
    }


def _ranked_key(evaluation: Mapping[str, Any]) -> Tuple[int, ...]:
    metrics = evaluation["metrics"]
    objective = metrics["objective_key"]
    return (
        int(metrics["maximum_path_hops"]),
        int(metrics["maximum_path_tdm_ratio_sum"]),
        round(float(objective[0]) / GAIN_QUANTUM),
        round(float(objective[1]) / GAIN_QUANTUM),
        int(objective[2]),
        int(objective[3]),
        int(objective[4]),
        int(objective[5]),
        int(objective[6]),
        int(objective[7]),
    )


def refine_partition_pressure_exhaustive(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    *,
    max_moves: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Reference global-best direct/ejection refinement for compact graphs."""

    current = dict(initial_assignment["cluster_assignment"])
    initial = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
    )
    current_evaluation = initial
    moves = []
    fixed = {
        cluster["cluster"]: cluster["fixed_fpga"]
        for cluster in model["clusters"]
    }
    fpga_ids = sorted(record["fpga"] for record in model["fpgas"])
    limit = len(current) if max_moves is None else max_moves
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("partition pressure max_moves is invalid")

    while len(moves) < limit:
        incumbent_key = _ranked_key(current_evaluation)
        candidates = []
        for cluster in sorted(current):
            if fixed[cluster] is not None:
                continue
            source = current[cluster]
            for target in fpga_ids:
                if target == source:
                    continue
                trial = dict(current)
                trial[cluster] = target
                try:
                    evaluation = evaluate_partition_pressure(
                        ir,
                        platform,
                        clusters_artifact,
                        constraints,
                        route_constraints,
                        model,
                        trial,
                    )
                except ValidationError:
                    continue
                ranked = _ranked_key(evaluation)
                if ranked >= incumbent_key:
                    continue
                candidates.append(
                    (ranked, 0, cluster, target, None, None, evaluation)
                )
        movable = [
            cluster for cluster in sorted(current) if fixed[cluster] is None
        ]
        for cluster in movable:
            source = current[cluster]
            for target in fpga_ids:
                if target == source:
                    continue
                for partner in movable:
                    if partner == cluster or current[partner] != target:
                        continue
                    for partner_target in fpga_ids:
                        if partner_target == target:
                            continue
                        trial = dict(current)
                        trial[cluster] = target
                        trial[partner] = partner_target
                        try:
                            evaluation = evaluate_partition_pressure(
                                ir,
                                platform,
                                clusters_artifact,
                                constraints,
                                route_constraints,
                                model,
                                trial,
                            )
                        except ValidationError:
                            continue
                        ranked = _ranked_key(evaluation)
                        if ranked >= incumbent_key:
                            continue
                        candidates.append(
                            (
                                ranked,
                                1,
                                cluster,
                                target,
                                partner,
                                partner_target,
                                evaluation,
                            )
                        )
        if not candidates:
            break
        (
            ranked,
            phase,
            cluster,
            target,
            partner,
            partner_target,
            selected,
        ) = min(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3],
                "" if item[4] is None else item[4],
                "" if item[5] is None else item[5],
            ),
        )
        source = current[cluster]
        partner_source = None if partner is None else current[partner]
        moves.append(
            {
                "index": len(moves),
                "kind": "move" if phase == 0 else "ejection",
                "phase": phase,
                "sweep": 0,
                "cluster": cluster,
                "source": source,
                "target": target,
                "partner": partner,
                "partner_source": partner_source,
                "partner_target": partner_target,
                "before_objective_key": current_evaluation["metrics"][
                    "objective_key"
                ],
                "after_objective_key": selected["metrics"]["objective_key"],
                "ranked_objective_key": list(ranked),
            }
        )
        current[cluster] = target
        if partner is not None:
            current[partner] = partner_target
        current_evaluation = selected

    final_assignment = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        current,
        provider=PARTITION_PRESSURE_PROVIDER,
        seed=initial_assignment.get("seed", 0),
        provider_metadata={
            "initial_provider": initial_assignment.get("provider"),
            "model_sha256": _canonical_digest(model),
        },
    )
    trace = {
        "schema": PARTITION_PRESSURE_TRACE_SCHEMA,
        "design": model["design"],
        "platform": model["platform"],
        "provider": PARTITION_PRESSURE_PROVIDER,
        "model_sha256": _canonical_digest(model),
        "initial_assignment_sha256": _canonical_digest(initial_assignment),
        "initial_metrics": initial["metrics"],
        "moves": moves,
        "final_metrics": current_evaluation["metrics"],
        "final_cluster_assignment_sha256": _canonical_digest(current),
    }
    return final_assignment, trace


def validate_partition_pressure_trace(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    final_assignment: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> Dict[str, Any]:
    if trace.get("schema") != PARTITION_PRESSURE_TRACE_SCHEMA:
        raise ValidationError("partition pressure trace schema is invalid")
    expected_assignment, expected_trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        initial_assignment,
        max_moves=len(trace.get("moves", [])),
    )
    if trace != expected_trace:
        raise ValidationError("partition pressure trace reconstruction mismatch")
    if final_assignment != expected_assignment:
        raise ValidationError("partition pressure final assignment mismatch")
    return {
        "status": "pass",
        "moves": len(expected_trace["moves"]),
        "initial_objective_key": expected_trace["initial_metrics"][
            "objective_key"
        ],
        "final_objective_key": expected_trace["final_metrics"][
            "objective_key"
        ],
    }


def run_partition_pressure_reference(
    ir_path: Path,
    platform_path: Path,
    clusters_path: Path,
    constraints_path: Path,
    timing_database_path: Path,
    route_constraints_path: Path,
    initial_assignment_path: Path,
    output_dir: Path,
    *,
    max_moves: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the compact exhaustive reference and write a checked bundle."""

    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    clusters = read_json(clusters_path)
    constraints = read_json(constraints_path)
    timing = read_json(timing_database_path)
    route_constraints = normalize_route_constraints(
        read_json(route_constraints_path), platform
    )
    initial = read_json(initial_assignment_path)
    validate_partition_artifacts(ir, platform, clusters, initial)
    model = build_partition_pressure_model(
        ir,
        platform,
        clusters,
        constraints,
        timing,
        route_constraints,
        timing_database_path=timing_database_path,
    )
    final, trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters,
        constraints,
        route_constraints,
        model,
        initial,
        max_moves=max_moves,
    )
    validation = validate_partition_pressure_trace(
        ir,
        platform,
        clusters,
        constraints,
        route_constraints,
        model,
        initial,
        final,
        trace,
    )
    report = {
        "schema": PARTITION_PRESSURE_REPORT_SCHEMA,
        "status": "pass",
        "design": model["design"],
        "platform": model["platform"],
        "provider": PARTITION_PRESSURE_PROVIDER,
        "qualification": "compact-exhaustive-reference",
        "validation": validation,
        "source_sha256": {
            "ir": _canonical_digest(ir.value),
            "platform": _canonical_digest(platform.to_dict()),
            "clusters": _canonical_digest(clusters),
            "constraints": _canonical_digest(constraints),
            "timing_database": _canonical_digest(timing),
            "route_constraints": _canonical_digest(route_constraints),
            "initial_assignment": _canonical_digest(initial),
        },
        "artifacts": {
            "model": "partition_pressure_model.json",
            "trace": "partition_pressure_trace.json",
            "assignment": "assignment.json",
            "report": "partition_pressure_report.json",
        },
    }
    write_json(output_dir / "partition_pressure_model.json", model)
    write_json(output_dir / "partition_pressure_trace.json", trace)
    write_json(output_dir / "assignment.json", final)
    write_json(output_dir / "partition_pressure_report.json", report)
    return report


def _native_dimensions(
    platform: Platform, model: Mapping[str, Any]
) -> List[str]:
    model_fpga_ids = {record["fpga"] for record in model["fpgas"]}
    partition_fpgas = [
        fpga for fpga in platform.fpgas if fpga.id in model_fpga_ids
    ]
    dimensions = ["cells"]
    dimensions.extend(
        field
        for field in RESOURCE_FIELDS
        if any(
            cluster["resources"].get(field, 0)
            for cluster in model["clusters"]
        )
        and all(
            fpga.effective_capacity.get(field, 0) > 0
            for fpga in partition_fpgas
        )
    )
    return dimensions


def _write_patron_native_input(
    path: Optional[Path],
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    max_moves: int,
    flow_version: int,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    _writer: Optional[Any] = None,
) -> Dict[str, Any]:
    if flow_version not in {6, 9, 10, 11, 14}:
        raise ValidationError("native PATRON algorithm version is invalid")
    flow_refinement = flow_version != 6
    use_physical_feedback = physical_feedback is not None
    if flow_version == 14 and (
        clusters_artifact.get("policy", {}).get("cut_mode")
        != CUT_MODE_STATIC_EXACT
    ):
        raise ValidationError(
            "native PATRON v14 requires generalized Static Exact clusters"
        )
    if use_physical_feedback:
        if flow_version != 11:
            raise ValidationError(
                "native PATRON physical feedback requires algorithm v11"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        physical_feedback_scale = _require_finite(
            physical_feedback_scale,
            "native PATRON physical feedback scale",
        )
        if physical_feedback_scale <= 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale must be positive"
            )
        feedback_by_index = {
            record["index"]: record for record in physical_feedback["paths"]
        }
    else:
        if flow_version == 11:
            raise ValidationError(
                "native PATRON v11 requires physical feedback"
            )
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale has no artifact"
            )
        feedback_by_index = {}
    parts = sorted(record["fpga"] for record in model["fpgas"])
    part_index = {part: index for index, part in enumerate(parts)}
    packed_nets = (
        model["nets"]
        if isinstance(model["nets"], _PackedPressureNetSequence)
        else None
    )
    if packed_nets is not None:
        clusters: Sequence[Mapping[str, Any]] = packed_nets.native_clusters()
        cluster_index: Mapping[str, int] = _PackedIndexPositions(
            packed_nets.native_cluster_identities
        )
        cluster_names: Sequence[str] = packed_nets.native_cluster_names()
    else:
        clusters = sorted(
            model["clusters"], key=lambda item: item["cluster"]
        )
        cluster_index = {
            cluster["cluster"]: index
            for index, cluster in enumerate(clusters)
        }
        cluster_names = [cluster["cluster"] for cluster in clusters]
    dimensions = _native_dimensions(platform, model)
    balance = validate_cluster_assignment_balance(
        platform,
        clusters_artifact["clusters"],
        initial_assignment["cluster_assignment"],
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
        constraints.get("active_fpgas"),
        constraints.get("balance_dimensions", ()),
        constraints.get("resource_reservations_by_fpga", {}),
    )
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    planning_capacity = planning_resource_capacities(
        [fpga_by_id[part] for part in parts],
        constraints.get("resource_reservations_by_fpga", {}),
    )
    domains = sorted(model["capacities"], key=lambda item: item["key"])
    domain_index = {
        domain["key"]: index for index, domain in enumerate(domains)
    }
    arc_domain = _capacity_key_by_arc(model)
    link_by_id = {link.id: link for link in platform.links}
    if packed_nets is not None:
        nets: Sequence[Mapping[str, Any]] = packed_nets
        net_index: Any = packed_nets
    else:
        nets = sorted(model["nets"], key=lambda item: item["net"])
        net_index = {
            net["net"]: index for index, net in enumerate(nets)
        }
    max_hops = model["configuration"]["max_route_hops"]
    if _writer is None:
        if path is None:
            raise ValidationError("native PATRON input path is missing")
        lines = _NativeBinaryFileWriter(path)
    else:
        if path is not None:
            raise ValidationError(
                "native PATRON input cannot use both a path and a stream"
            )
        lines = _writer
    published = False
    try:
        physical_hop_guard_scale_ns = (
            PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS
            if flow_version == 10
            else 0.0
        )
        flow_configuration = (
            (
                len(clusters),
                PATRON_FLOW_CORRIDOR_DISTANCE,
                PATRON_FLOW_PIERCING_STRATEGY,
                PATRON_FLOW_MAX_LEGAL_CANDIDATES,
                PATRON_FLOW_MAX_POLISH_MOVES,
                PATRON_FLOW_MAX_FRONTIER_PATHS,
                PATRON_FLOW_MAX_TAIL_MOVES,
            )
            if flow_refinement
            else (0, 0, 0, 0, 0, 0, 0)
        )
        lines.header(
            flow_version=flow_version,
            use_physical_feedback=use_physical_feedback,
            counts=(
                len(parts),
                len(clusters),
                len(dimensions),
                len(domains),
                len(nets),
                len(model["paths"]),
            ),
            integers=(
                -1 if max_hops is None else max_hops,
                model["configuration"]["tdm_max_ratio"],
                model["configuration"]["tdm_ratio_quantum"],
                constraints["min_used_fpgas"],
                max_moves,
                model["configuration"]["max_scalable_sweeps"],
                model["configuration"][
                    "scalable_ejection_critical_limit"
                ],
                model["configuration"][
                    "scalable_ejection_donor_limit"
                ],
                model["configuration"]["max_scalable_ejections"],
                route_constraints.get("tdm_min_ratio", 1),
            ),
            scalars=(
                model["normalization"]["positive_slack_scale_ns"],
                model["normalization"]["negative_slack_scale_ns"],
                model["normalization"]["max_clock_period_ns"],
                model["configuration"][
                    "boundary_fanout_penalty_scale_ns"
                ],
                physical_hop_guard_scale_ns,
            ),
            flow_configuration=flow_configuration,
        )
        total_cells = sum(cluster["cells"] for cluster in clusters)
        for part in parts:
            hard = []
            allowed = []
            for dimension in dimensions:
                hard.append(
                    float(total_cells)
                    if dimension == "cells"
                    else float(planning_capacity[part][dimension])
                )
                allowed.append(
                    float(balance["balance_allowed_loads"][part][dimension])
                )
            lines.cap(part_index[part], hard, allowed)
        for index, cluster in enumerate(clusters):
            current = initial_assignment["cluster_assignment"][cluster["cluster"]]
            fixed = cluster["fixed_fpga"]
            weights = [
                cluster["cells"]
                if dimension == "cells"

                else cluster["resources"].get(dimension, 0)
                for dimension in dimensions
            ]
            current_index = part_index[current]
            fixed_index = -1 if fixed is None else part_index[fixed]
            lines.cluster(index, current_index, fixed_index, weights)
        for index, domain in enumerate(domains):
            link = link_by_id[domain["link"]]
            is_sll = domain["link"] in set(route_constraints["sll_links"])
            ratio_delay_slope_ns = route_link_delay_coefficients_ns(
                platform,
                link.id,
                link.endpoints[0],
                link.endpoints[1],
                route_constraints,
            )[1]
            opposite = domain_index.get(
                domain.get("opposite_capacity_key"), -1
            )
            banks = domain.get("bank_count", 0)
            wires = domain.get("wires_per_bank", 0)
            capacity_bits = int(domain["capacity_bits"])
            lines.domain(
                index,
                domain["lanes"],
                is_sll,
                ratio_delay_slope_ns,
                opposite,
                banks,
                wires,
                capacity_bits,
            )
        for source in parts:
            for sink in parts:
                route = model["shortest_routes"][source][sink]
                if route is None:
                    lines.route(
                        part_index[source], part_index[sink], None
                    )
                    continue
                route_fields = []
                for arc in route:
                    route_fields.append(
                        (
                            domain_index[
                                arc_domain[
                                    (arc["link"], arc["from"], arc["to"])
                                ]
                            ],
                            route_link_delay_ns(
                                platform,
                                arc["link"],
                                arc["from"],
                                arc["to"],
                                route_constraints,
                            ),
                        )
                    )
                lines.route(
                    part_index[source],
                    part_index[sink],
                    route_fields,
                )
        if packed_nets is not None:
            for index in range(len(packed_nets)):
                drivers, sinks = packed_nets.native_net_members(index)
                driver_ordinals = (
                    packed_nets.native_cluster_ordinal(item)
                    for item in drivers
                )
                sink_ordinals = (
                    packed_nets.native_cluster_ordinal(item)
                    for item in sinks
                )
                lines.net(
                    index,
                    len(drivers),
                    driver_ordinals,
                    len(sinks),
                    sink_ordinals,
                )
        else:
            for index, net in enumerate(nets):
                drivers = [cluster_index[item] for item in net["drivers"]]
                sinks = [cluster_index[item] for item in net["sinks"]]
                lines.net(
                    index,
                    len(drivers),
                    drivers,
                    len(sinks),
                    sinks,
                )
        packed_paths = (
            model["paths"]
            if isinstance(model["paths"], _PackedPressurePathSequence)
            else None
        )
        for index in range(len(model["paths"])):
            if packed_paths is not None:
                (
                    clock_period_ns,
                    base_slack_ns,
                    start_cluster_index,
                    end_cluster_index,
                    path_nets,
                ) = packed_paths.native_path(index)
            else:
                timing_path = model["paths"][index]
                clock_period_ns = timing_path["clock_period_ns"]
                base_slack_ns = timing_path["base_slack_ns"]
                start_cluster = timing_path.get("start_cluster")
                end_cluster = timing_path.get("end_cluster")
                start_cluster_index = (
                    -1
                    if start_cluster is None
                    else cluster_index[start_cluster]
                )
                end_cluster_index = (
                    -1
                    if end_cluster is None
                    else cluster_index[end_cluster]
                )
            if packed_paths is None and packed_nets is not None:
                path_nets = []
                for net in timing_path["path_nets"]:
                    ordinal = packed_nets.ordinal(net)
                    if ordinal is not None:
                        path_nets.append(ordinal)
            elif packed_paths is None:
                path_nets = [
                    net_index[net]
                    for net in timing_path["path_nets"]
                    if net in net_index
                ]
            feedback: Optional[Tuple[int, int, float]] = None
            if use_physical_feedback:
                feedback_record = feedback_by_index.get(index)
                if feedback_record is None:
                    feedback = (-1, -1, 0.0)
                else:
                    feedback = (
                        part_index[
                            feedback_record["observed_source_fpga"]
                        ],
                        part_index[
                            feedback_record["observed_sink_fpga"]
                        ],
                        float(feedback_record["positive_residual_ns"])
                        * physical_feedback_scale,
                    )
            lines.path(
                index,
                clock_period_ns,
                base_slack_ns,
                start_cluster_index,
                end_cluster_index,
                path_nets,
                feedback,
            )
        lines.publish()
        published = True
    finally:
        if not published:
            lines.abort()
    return {
        "parts": parts,
        "clusters": cluster_names,
        "input": dict(lines.summary),
    }


def _metrics_from_objective(
    values: List[float], *, path_transition_objective: bool = False
) -> Dict[str, Any]:
    if len(values) != 8 or not all(math.isfinite(value) for value in values):
        raise ValidationError("native PATRON objective is invalid")
    result = {
        "worst_normalized_slack": -values[0],
        "total_negative_normalized_slack": -values[1],
        "negative_paths": int(values[2]),
        "maximum_predicted_tdm_ratio": int(values[3]),
        "maximum_capacity_domain_load": int(values[4]),
        "total_bit_hops": int(values[6]),
        "cut_bits": int(values[7]),
        "objective_key": values,
    }
    result[
        "total_path_partition_transitions"
        if path_transition_objective
        else "total_path_snaking"
    ] = int(values[5])
    return result


def _parse_patron_typed_output(
    path: Path,
    indexes: Mapping[str, Any],
    *,
    packed_assignment: bool = False,
) -> Tuple[
    Mapping[str, str],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    str,
    Dict[str, Any],
]:
    """Read the summary-only fixed-width PATRON result without text rows."""

    clusters = indexes.get("clusters")
    parts = indexes.get("parts")
    if (
        not isinstance(clusters, Sequence)
        or isinstance(clusters, (str, bytes))
        or not isinstance(parts, Sequence)
        or isinstance(parts, (str, bytes))
    ):
        raise ValidationError("native PATRON typed output indexes are invalid")
    stream = path.open("rb")
    try:
        try:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as error:
            raise ValidationError(
                "native PATRON typed output is unreadable"
            ) from error
        try:
            offset = 0

            def take(format_string: str) -> Tuple[Any, ...]:
                nonlocal offset
                size = struct.calcsize(format_string)
                if offset + size > len(data):
                    raise ValidationError(
                        "native PATRON typed output is truncated"
                    )
                values = struct.unpack_from(format_string, data, offset)
                offset += size
                return values

            magic, version, output_version, mode_size = take("<QIII")
            if (
                magic != _PATRON_TYPED_OUTPUT_MAGIC
                or version != _PATRON_TYPED_OUTPUT_VERSION
                or output_version not in {6, 7, 8, 9, 10, 11, 14}
                or mode_size > 256
                or offset + mode_size > len(data)
            ):
                raise ValidationError(
                    "native PATRON typed output header is invalid"
                )
            try:
                mode = bytes(data[offset : offset + mode_size]).decode("ascii")
            except UnicodeDecodeError as error:
                raise ValidationError(
                    "native PATRON typed output mode is invalid"
                ) from error
            offset += mode_size
            expected_modes = (
                {
                    "endpoint-exact-global-best-v6",
                    "endpoint-exact-critical-ejection-v6",
                }
                if output_version == 6
                else {f"endpoint-exact-critical-flow-v{output_version}"}
            )
            if mode not in expected_modes:
                raise ValidationError(
                    "native PATRON typed output mode is invalid"
                )
            initial_values = list(take("<8d"))
            final_values = list(take("<8d"))
            capacity = take("<4q")
            path_max = take("<4q")
            move_count, batch_count, change_count, detail = take("<3QI")
            cluster_count = take("<Q")[0]
            if (
                detail != 0
                or cluster_count != len(clusters)
                or any(value < 0 for value in capacity)
                or any(value < 0 for value in path_max)
                or len(data) - offset != 4 * cluster_count + 4
            ):
                raise ValidationError(
                    "native PATRON typed output summary is invalid"
                )
            sentinel = 2**32 - 1
            packed_parts = (
                array("I", [sentinel]) * cluster_count
                if packed_assignment
                else None
            )
            assignment: Dict[str, str] = {}
            for native_cluster in range(cluster_count):
                part = take("<I")[0]
                if part >= len(parts):
                    raise ValidationError(
                        "native PATRON typed output assignment is invalid"
                    )
                cluster_id = clusters[native_cluster]
                part_id = parts[part]
                if not isinstance(cluster_id, str) or not isinstance(
                    part_id, str
                ):
                    raise ValidationError(
                        "native PATRON typed output identity is invalid"
                    )
                if packed_parts is None:
                    if cluster_id in assignment:
                        raise ValidationError(
                            "native PATRON typed output assignment repeats"
                        )
                    assignment[cluster_id] = part_id
                else:
                    if (
                        not cluster_id.startswith("c")
                        or not cluster_id[1:].isdigit()
                    ):
                        raise ValidationError(
                            "native PATRON cluster identity is not canonical"
                        )
                    cluster_ordinal = int(cluster_id[1:])
                    if (
                        cluster_ordinal >= len(packed_parts)
                        or _PackedClusterAssignment.identity(cluster_ordinal)
                        != cluster_id
                        or packed_parts[cluster_ordinal] != sentinel
                    ):
                        raise ValidationError(
                            "native PATRON typed output assignment repeats"
                        )
                    packed_parts[cluster_ordinal] = part
            if len(data) - offset != 4:
                raise ValidationError(
                    "native PATRON typed output has trailing bytes"
                )
            expected_crc32 = struct.unpack_from("<I", data, offset)[0]
            checksum = 0
            for start in range(0, offset, 1024 * 1024):
                checksum = zlib.crc32(
                    data[start : min(start + 1024 * 1024, offset)],
                    checksum,
                )
            if checksum != expected_crc32:
                raise ValidationError(
                    "native PATRON typed output checksum disagrees"
                )
            offset += 4
            final_assignment: Mapping[str, str] = (
                assignment
                if packed_parts is None
                else _PackedClusterAssignment(parts, packed_parts)
            )
            initial_metrics = _metrics_from_objective(
                initial_values,
                path_transition_objective=output_version == 14,
            )
            final_metrics = _metrics_from_objective(
                final_values,
                path_transition_objective=output_version == 14,
            )
            initial_metrics.update(
                {
                    "transport_capacity_violations": capacity[0],
                    "transport_capacity_excess_bits": capacity[1],
                    "maximum_path_hops": path_max[0],
                    "maximum_path_tdm_ratio_sum": path_max[1],
                }
            )
            final_metrics.update(
                {
                    "transport_capacity_violations": capacity[2],
                    "transport_capacity_excess_bits": capacity[3],
                    "maximum_path_hops": path_max[2],
                    "maximum_path_tdm_ratio_sum": path_max[3],
                }
            )
            return (
                final_assignment,
                [],
                [],
                initial_metrics,
                final_metrics,
                mode,
                {
                    "moves": move_count,
                    "batches": batch_count,
                    "changes": change_count,
                    "detail_retained": False,
                    "output_bytes": path.stat().st_size,
                    "output_transport": "typed-binary-file",
                    "output_crc32": checksum,
                },
            )
        finally:
            data.close()
    finally:
        stream.close()


def _parse_patron_native_output_text(
    path: Path,
    indexes: Mapping[str, Any],
    *,
    packed_assignment: bool = False,
) -> Tuple[
    Mapping[str, str],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    str,
    Dict[str, Any],
]:
    allowed_headers = (
        "EMUFLOW_PATRON_OUTPUT_V6",
        "EMUFLOW_PATRON_OUTPUT_V7",
        "EMUFLOW_PATRON_OUTPUT_V8",
        "EMUFLOW_PATRON_OUTPUT_V9",
        "EMUFLOW_PATRON_OUTPUT_V10",
        "EMUFLOW_PATRON_OUTPUT_V11",
        "EMUFLOW_PATRON_OUTPUT_V14",
    )
    stream = path.open("r", encoding="utf-8")
    output_version = stream.readline().rstrip("\r\n")
    if output_version not in allowed_headers:
        stream.close()
        raise ValidationError("native PATRON output header is invalid")
    path_transition_objective = output_version == "EMUFLOW_PATRON_OUTPUT_V14"
    def indexed(label: str, values: List[str], index: int) -> str:
        if index < 0 or index >= len(values):
            raise ValidationError(
                f"native PATRON {label} index is out of range"
            )
        return values[index]

    moves = []
    batches = []
    assignment: Dict[str, str] = {}
    packed_sentinel = 2**32 - 1
    packed_parts = (
        array("I", [packed_sentinel]) * len(indexes["clusters"])
        if packed_assignment
        else None
    )
    final_metrics = None
    initial_metrics = None
    mode = None
    summary = None
    capacity_summary = None
    path_max_summary = None
    ended = False
    next_assignment = 0
    try:
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            if ended:
                raise ValidationError(
                    "native PATRON output has records after END"
                )
            fields = line.split()
            if not fields:
                raise ValidationError(
                    "native PATRON output has an empty record"
                )
            if fields[0] == "MODE" and len(fields) == 2:
                if mode is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate MODE"
                    )
                mode = fields[1]
            elif fields[0] == "INITIAL" and len(fields) == 9:
                if initial_metrics is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate INITIAL"
                    )
                initial_metrics = _metrics_from_objective(
                    [float(item) for item in fields[1:]],
                    path_transition_objective=path_transition_objective,
                )
            elif fields[0] == "STEP" and len(fields) in (34, 36):
                (
                    index,
                    phase,
                    sweep,
                    cluster,
                    source,
                    target,
                    partner,
                    partner_source,
                    partner_target,
                ) = map(int, fields[1:10])
                if index != len(moves):
                    raise ValidationError(
                        "native PATRON move indexes are invalid"
                    )
                if phase not in (0, 1) or (phase == 0) != (partner < 0):
                    raise ValidationError(
                        "native PATRON step phase is invalid"
                    )
                before = [float(item) for item in fields[10:18]]
                after = [float(item) for item in fields[18:26]]
                ranked = [int(item) for item in fields[26:]]
                moves.append(
                    {
                        "index": index,
                        "kind": "move" if phase == 0 else "ejection",
                        "phase": phase,
                        "sweep": sweep,
                        "cluster": indexed(
                            "cluster", indexes["clusters"], cluster
                        ),
                        "source": indexed(
                            "part", indexes["parts"], source
                        ),
                        "target": indexed(
                            "part", indexes["parts"], target
                        ),
                        "partner": (
                            None
                            if partner < 0
                            else indexed(
                                "partner cluster",
                                indexes["clusters"],
                                partner,
                            )
                        ),
                        "partner_source": (
                            None
                            if partner_source < 0
                            else indexed(
                                "partner part",
                                indexes["parts"],
                                partner_source,
                            )
                        ),
                        "partner_target": (
                            None
                            if partner_target < 0
                            else indexed(
                                "partner part",
                                indexes["parts"],
                                partner_target,
                            )
                        ),
                        "before_objective_key": before,
                        "after_objective_key": after,
                        "ranked_objective_key": ranked,
                    }
                )
            elif fields[0] == "BATCH" and len(fields) in (27, 29):
                if output_version not in (
                    "EMUFLOW_PATRON_OUTPUT_V7",
                    "EMUFLOW_PATRON_OUTPUT_V8",
                    "EMUFLOW_PATRON_OUTPUT_V9",
                    "EMUFLOW_PATRON_OUTPUT_V10",
                    "EMUFLOW_PATRON_OUTPUT_V11",
                    "EMUFLOW_PATRON_OUTPUT_V14",
                ):
                    raise ValidationError(
                        "native PATRON v6 returned a BATCH"
                    )
                index, change_count = map(int, fields[1:3])
                if index != len(batches) or change_count <= 0:
                    raise ValidationError(
                        "native PATRON batch header is invalid"
                    )
                batches.append(
                    {
                        "index": index,
                        "changes": [],
                        "expected_changes": change_count,
                        "before_objective_key": [
                            float(item) for item in fields[3:11]
                        ],
                        "after_objective_key": [
                            float(item) for item in fields[11:19]
                        ],
                        "ranked_objective_key": [
                            int(item) for item in fields[19:]
                        ],
                    }
                )
            elif fields[0] == "CHANGE" and len(fields) == 5:
                if output_version not in (
                    "EMUFLOW_PATRON_OUTPUT_V7",
                    "EMUFLOW_PATRON_OUTPUT_V8",
                    "EMUFLOW_PATRON_OUTPUT_V9",
                    "EMUFLOW_PATRON_OUTPUT_V10",
                    "EMUFLOW_PATRON_OUTPUT_V11",
                    "EMUFLOW_PATRON_OUTPUT_V14",
                ):
                    raise ValidationError(
                        "native PATRON v6 returned a CHANGE"
                    )
                batch, cluster, source, target = map(int, fields[1:])
                if batch < 0 or batch >= len(batches):
                    raise ValidationError(
                        "native PATRON batch change is orphaned"
                    )
                record = batches[batch]
                if len(record["changes"]) >= record["expected_changes"]:
                    raise ValidationError(
                        "native PATRON batch has extra changes"
                    )
                record["changes"].append(
                    {
                        "cluster": indexed(
                            "cluster", indexes["clusters"], cluster
                        ),
                        "source": indexed(
                            "part", indexes["parts"], source
                        ),
                        "target": indexed(
                            "part", indexes["parts"], target
                        ),
                    }
                )
            elif fields[0] == "FINAL" and len(fields) == 9:
                if final_metrics is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate FINAL"
                    )
                final_metrics = _metrics_from_objective(
                    [float(item) for item in fields[1:]],
                    path_transition_objective=path_transition_objective,
                )
            elif fields[0] == "CAPACITY" and len(fields) == 5:
                if capacity_summary is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate CAPACITY"
                    )
                capacity_summary = [int(item) for item in fields[1:]]
                if any(value < 0 for value in capacity_summary):
                    raise ValidationError(
                        "native PATRON transport capacity summary is invalid"
                    )
            elif fields[0] == "PATH_MAX" and len(fields) == 5:
                if path_max_summary is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate PATH_MAX"
                    )
                path_max_summary = [int(item) for item in fields[1:]]
                if any(value < 0 for value in path_max_summary):
                    raise ValidationError(
                        "native PATRON path maximum summary is invalid"
                    )
            elif fields[0] == "SUMMARY" and len(fields) == 5:
                if summary is not None:
                    raise ValidationError(
                        "native PATRON returned duplicate SUMMARY"
                    )
                move_count, batch_count, change_count, detail = map(
                    int, fields[1:]
                )
                if (
                    move_count < 0
                    or batch_count < 0
                    or change_count < 0
                    or detail not in (0, 1)
                ):
                    raise ValidationError(
                        "native PATRON output summary is invalid"
                    )
                summary = {
                    "moves": move_count,
                    "batches": batch_count,
                    "changes": change_count,
                    "detail_retained": bool(detail),
                }
            elif fields[0] == "ASSIGN" and len(fields) == 3:
                cluster, part = map(int, fields[1:])
                cluster_id = indexed(
                    "cluster", indexes["clusters"], cluster
                )
                if packed_parts is None and cluster_id in assignment:
                    raise ValidationError(
                        "native PATRON returned duplicate ASSIGN"
                    )
                if cluster != next_assignment:
                    raise ValidationError(
                        "native PATRON ASSIGN order is invalid"
                    )
                part_id = indexed("part", indexes["parts"], part)
                if packed_parts is None:
                    assignment[cluster_id] = part_id
                else:
                    if (
                        not cluster_id.startswith("c")
                        or not cluster_id[1:].isdigit()
                    ):
                        raise ValidationError(
                            "native PATRON cluster identity is not canonical"
                        )
                    cluster_ordinal = int(cluster_id[1:])
                    if (
                        cluster_ordinal < 0
                        or cluster_ordinal >= len(packed_parts)
                        or _PackedClusterAssignment.identity(cluster_ordinal)
                        != cluster_id
                    ):
                        raise ValidationError(
                            "native PATRON cluster identity is not canonical"
                        )
                    if packed_parts[cluster_ordinal] != packed_sentinel:
                        raise ValidationError(
                            "native PATRON returned duplicate ASSIGN"
                        )
                    packed_parts[cluster_ordinal] = part
                next_assignment += 1
            elif fields == ["END"]:
                ended = True
            else:
                raise ValidationError(
                    f"native PATRON output record is invalid: {line}"
                )
    finally:
        stream.close()
    if not ended:
        raise ValidationError("native PATRON output END is missing")
    if packed_parts is None:
        if len(assignment) != len(indexes["clusters"]):
            raise ValidationError(
                "native PATRON assignment coverage is invalid"
            )
        final_assignment: Mapping[str, str] = assignment
    else:
        if (
            next_assignment != len(indexes["clusters"])
            or packed_sentinel in packed_parts
        ):
            raise ValidationError(
                "native PATRON assignment coverage is invalid"
            )
        final_assignment = _PackedClusterAssignment(
            indexes["parts"], packed_parts
        )
    for batch in batches:
        expected_changes = batch.pop("expected_changes")
        if len(batch["changes"]) != expected_changes:
            raise ValidationError(
                "native PATRON batch change coverage is invalid"
            )
    actual_changes = sum(len(batch["changes"]) for batch in batches)
    if summary is None:
        summary = {
            "moves": len(moves),
            "batches": len(batches),
            "changes": actual_changes,
            "detail_retained": True,
        }
    elif summary["detail_retained"]:
        if summary != {
            "moves": len(moves),
            "batches": len(batches),
            "changes": actual_changes,
            "detail_retained": True,
        }:
            raise ValidationError(
                "native PATRON detailed output does not match SUMMARY"
            )
    elif moves or batches:
        raise ValidationError(
            "native PATRON summary-only output contains trace records"
        )
    if final_metrics is None or initial_metrics is None or mode is None:
        raise ValidationError("native PATRON output metadata is incomplete")
    if capacity_summary is not None:
        initial_metrics.update(
            {
                "transport_capacity_violations": capacity_summary[0],
                "transport_capacity_excess_bits": capacity_summary[1],
            }
        )
        final_metrics.update(
            {
                "transport_capacity_violations": capacity_summary[2],
                "transport_capacity_excess_bits": capacity_summary[3],
            }
        )
    if path_max_summary is not None:
        initial_metrics.update(
            {
                "maximum_path_hops": path_max_summary[0],
                "maximum_path_tdm_ratio_sum": path_max_summary[1],
            }
        )
        final_metrics.update(
            {
                "maximum_path_hops": path_max_summary[2],
                "maximum_path_tdm_ratio_sum": path_max_summary[3],
            }
        )
    summary["output_bytes"] = path.stat().st_size
    summary["output_transport"] = "text-record-stream"
    return (
        final_assignment,
        moves,
        batches,
        initial_metrics,
        final_metrics,
        mode,
        summary,
    )


def _parse_patron_native_output(
    path: Path,
    indexes: Mapping[str, Any],
    *,
    packed_assignment: bool = False,
) -> Tuple[
    Mapping[str, str],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    str,
    Dict[str, Any],
]:
    try:
        with path.open("rb") as stream:
            prefix = stream.read(8)
    except OSError as error:
        raise ValidationError("native PATRON output is unreadable") from error
    if prefix.startswith(b"EMUFLOW_"):
        return _parse_patron_native_output_text(
            path, indexes, packed_assignment=packed_assignment
        )
    return _parse_patron_typed_output(
        path, indexes, packed_assignment=packed_assignment
    )


def run_partition_pressure_native(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    max_moves: Optional[int] = None,
    algorithm_version: int = DEFAULT_PATRON_ALGORITHM_VERSION,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    output_validation: str = "full",
    retain_trace_seals: bool = True,
    stream_semantic_records: bool = False,
    timeout_seconds: int = 3600,
    _release_model_before_assignment: bool = False,
    _defer_candidate_derived_records: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from .phase3_pressure_native import NativePressureModel
    store_model = isinstance(model, NativePressureModel)
    limit = len(model["clusters"]) if max_moves is None else max_moves
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("native PATRON max_moves is invalid")
    if output_validation not in {"full", "online", "caller"}:
        raise ValidationError("native PATRON output validation mode is invalid")
    if not isinstance(retain_trace_seals, bool):
        raise ValidationError("native PATRON trace-seal flag is invalid")
    if not isinstance(stream_semantic_records, bool):
        raise ValidationError("native PATRON record-stream flag is invalid")
    if not isinstance(_release_model_before_assignment, bool):
        raise ValidationError("native PATRON model-release flag is invalid")
    if not isinstance(_defer_candidate_derived_records, bool):
        raise ValidationError(
            "native PATRON candidate derived-record deferral is invalid"
        )
    if _release_model_before_assignment and (
        retain_trace_seals or not stream_semantic_records
    ):
        raise ValidationError(
            "native PATRON model release requires summary-only streamed output"
        )
    if _defer_candidate_derived_records and (
        not _release_model_before_assignment
        or output_validation != "caller"
    ):
        raise ValidationError(
            "native PATRON candidate derived-record deferral requires the "
            "production caller-validation path"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValidationError("native PATRON timeout is invalid")
    trace_model = {
        "design": model["design"],
        "platform": model["platform"],
        "clusters": len(model["clusters"]),
        "configuration": {
            key: model["configuration"][key]
            for key in (
                "max_scalable_sweeps",
                "scalable_ejection_critical_limit",
                "scalable_ejection_donor_limit",
                "max_scalable_ejections",
            )
        },
    }
    if (
        isinstance(algorithm_version, bool)
        or not isinstance(algorithm_version, int)
        or algorithm_version not in {6, 9, 10, 11, 14}
    ):
        raise ValidationError("native PATRON algorithm version is invalid")
    flow_refinement = algorithm_version != 6
    use_physical_feedback = physical_feedback is not None
    if store_model and (algorithm_version != 14 or retain_trace_seals or physical_feedback is not None
                        or (executable is not None and not is_unified_native_executable(executable))
                        or not stream_semantic_records):
        raise ValidationError("native store model requires unified v14 summary production")
    if use_physical_feedback:
        if algorithm_version != 11:
            raise ValidationError(
                "native PATRON physical feedback requires algorithm v11"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        physical_feedback_scale = _require_finite(
            physical_feedback_scale,
            "native PATRON physical feedback scale",
        )
        if physical_feedback_scale <= 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale must be positive"
            )
        feedback_sha256 = _canonical_digest(physical_feedback)
    else:
        if algorithm_version == 11:
            raise ValidationError(
                "native PATRON v11 requires physical feedback"
            )
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale has no artifact"
            )
        feedback_sha256 = None
    native_command = ([] if store_model else resolve_native_stage_command(
        "phase3", "refine-patron", executable
    ))
    with tempfile.TemporaryDirectory(prefix="emuflow-patron-") as temporary:
        root = Path(temporary)
        native_output = root / "patron.out"
        native_input_copy = os.environ.get("EMUFLOW_PATRON_INPUT_COPY")
        environment = dict(os.environ)
        for name in tuple(environment):
            # A container executable binding is transport configuration, not
            # one of the retired implicit algorithm overrides.
            if (name.startswith("EMUFLOW_PATRON_") and
                    name != "EMUFLOW_PATRON_CONTAINER_BINARY"):
                del environment[name]
        command = list(native_command)
        if not retain_trace_seals:
            command.extend(("--summary-output", "--typed-output"))
        elif len(native_command) > 1:
            command.append("--trace-output")
        if store_model:
            if native_input_copy:
                raise ValidationError("native store model does not materialize a legacy input copy")
            indexes = phase3_runtime_call("patron.build_and_refine_native_model", model.execute,
                root, native_output, initial_assignment, limit, timeout_seconds, environment, executable=executable)
            completed = subprocess.CompletedProcess([], 0, "", "")
        elif native_input_copy:
            copy_path = Path(native_input_copy)
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            indexes = phase3_runtime_call(
                "patron.export_native_input",
                _write_patron_native_input,
                copy_path,
                platform,
                clusters_artifact,
                constraints,
                route_constraints,
                model,
                initial_assignment,
                limit,
                algorithm_version,
                physical_feedback,
                physical_feedback_scale,
            )
            command.extend(
                ("--typed-input", str(copy_path), str(native_output))
            )
            try:
                completed = phase3_runtime_call(
                    "patron.execute_native",
                    subprocess.run,
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise ValidationError(
                    "native PATRON exceeded timeout of "
                    f"{timeout_seconds} seconds"
                ) from error
        else:
            command.extend(("--typed-input", "-", str(native_output)))
            stdout_path = root / "patron.stdout"
            stderr_path = root / "patron.stderr"
            with (
                stdout_path.open("w+", encoding="utf-8") as stdout_stream,
                stderr_path.open("w+", encoding="utf-8") as stderr_stream,
            ):
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    env=environment,
                )
                if process.stdin is None:
                    process.kill()
                    process.wait()
                    raise ValidationError(
                        "native PATRON input stream is unavailable"
                    )
                try:
                    indexes = phase3_runtime_call(
                        "patron.export_native_input",
                        _write_patron_native_input,
                        None,
                        platform,
                        clusters_artifact,
                        constraints,
                        route_constraints,
                        model,
                        initial_assignment,
                        limit,
                        algorithm_version,
                        physical_feedback,
                        physical_feedback_scale,
                        _writer=_NativeBinaryPipeWriter(process.stdin),
                    )
                except (BrokenPipeError, OSError) as error:
                    process.stdin = None
                    returncode = process.wait()
                    stderr_stream.flush()
                    stderr_stream.seek(0)
                    detail = stderr_stream.read().strip()
                    raise ValidationError(
                        "native PATRON rejected its streamed input with exit "
                        f"code {returncode}"
                        + (f": {detail}" if detail else "")
                    ) from error
                except BaseException:
                    process.stdin = None
                    process.kill()
                    process.wait()
                    raise
                process.stdin = None
                try:
                    returncode = phase3_runtime_call(
                        "patron.execute_native",
                        process.wait,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.wait()
                    raise ValidationError(
                        "native PATRON exceeded timeout of "
                        f"{timeout_seconds} seconds"
                    ) from error
                stdout_stream.flush()
                stderr_stream.flush()
                stdout_stream.seek(0)
                stderr_stream.seek(0)
                completed = subprocess.CompletedProcess(
                    command,
                    returncode,
                    stdout_stream.read(),
                    stderr_stream.read(),
                )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ValidationError(
                "native PATRON failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        try:
            (
                cluster_assignment, moves, batches, initial_metrics, final_metrics, mode, output_summary,
            ) = phase3_runtime_call(
                "patron.parse_native_output", _parse_patron_native_output,
                native_output, indexes, packed_assignment=stream_semantic_records,
            )
        finally:
            if store_model:
                indexes["clusters"].close()
        output_summary["input"] = indexes["input"]
    provider_metadata = {
        "initial_provider": initial_assignment.get("provider"),
    }
    if algorithm_version == 14:
        provider_metadata.update(
            {
                "partition_search_space": (
                    "native-provider-neutral-generalized-v1"
                ),
                "candidate_selection": (
                    "structural-legality-and-predictive-timing-v1"
                ),
            }
        )
    model_sha256 = None
    if retain_trace_seals:
        model_sha256 = _canonical_digest(model)
        provider_metadata["model_sha256"] = model_sha256
    if _release_model_before_assignment:
        if not isinstance(model, dict):
            raise ValidationError(
                "native PATRON production model is not releasable"
            )
        model.clear()
        phase3_runtime_call("patron.release_pressure_model", gc.collect)
    final = phase3_runtime_call(
        "patron.build_candidate_assignment",
        build_partition_assignment,
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=(
            {
                6: PARTITION_PRESSURE_NATIVE_PROVIDER,
                9: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
                10: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
                11: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
                14: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V14,
            }[algorithm_version]
        ),
        seed=initial_assignment.get("seed", 0),
        provider_metadata=provider_metadata,
        _include_semantic_contract=not _defer_candidate_derived_records,
        _stream_semantic_records=stream_semantic_records,
        _defer_derived_records=_defer_candidate_derived_records,
    )
    if output_validation == "full":
        validate_partition_artifacts(ir, platform, clusters_artifact, final)
    elif output_validation == "online":
        validate_partition_artifacts_online(platform, clusters_artifact, final)
    trace = {
        "schema": (
            {
                6: PARTITION_PRESSURE_TRACE_SCHEMA,
                9: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
                10: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
                11: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
                14: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V14,
            }[algorithm_version]
        ),
        "design": trace_model["design"],
        "platform": trace_model["platform"],
        "provider": (
            {
                6: PARTITION_PRESSURE_NATIVE_PROVIDER,
                9: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
                10: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
                11: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
                14: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V14,
            }[algorithm_version]
        ),
        "mode": mode,
        "configuration": {
            "algorithm_version": algorithm_version,
            "max_moves": limit,
            "max_scalable_sweeps": trace_model["configuration"][
                "max_scalable_sweeps"
            ],
            "scalable_ejection_critical_limit": trace_model["configuration"][
                "scalable_ejection_critical_limit"
            ],
            "scalable_ejection_donor_limit": trace_model["configuration"][
                "scalable_ejection_donor_limit"
            ],
            "max_scalable_ejections": trace_model["configuration"][
                "max_scalable_ejections"
            ],
            "flow_refinement": _flow_refinement_configuration(
                flow_refinement,
                trace_model["clusters"],
                version=(
                    {6: 1, 9: 3, 10: 4, 11: 5, 14: 8}[
                        algorithm_version
                    ]
                ),
                physical_feedback_sha256=feedback_sha256,
                physical_feedback_scale=(
                    physical_feedback_scale
                    if use_physical_feedback
                    else 0.0
                ),
            ),
        },
        **(
            {"physical_feedback_sha256": feedback_sha256}
            if use_physical_feedback
            else {}
        ),
        "initial_metrics": initial_metrics,
        "move_count": output_summary["moves"],
        "batch_count": output_summary["batches"],
        "change_count": output_summary["changes"],
        "native_input_transport": output_summary["input"]["transport"],
        "native_input_bytes": output_summary["input"]["bytes"],
        "native_input_records": output_summary["input"]["records"],
        "native_input_crc32": output_summary["input"].get("crc32"),
        "native_output_transport": output_summary["output_transport"],
        "native_output_bytes": output_summary["output_bytes"],
        "native_output_crc32": output_summary.get("output_crc32"),
        "trace_storage": (
            "retained"
            if output_summary["detail_retained"]
            else "summary-only"
        ),
        "final_metrics": final_metrics,
    }
    if store_model:
        trace["model_implementation_id"] = model.implementation_id
        trace["model_counts"] = output_summary["input"]["model"]
        trace["native_model_metrics"] = output_summary["input"]["metrics"]
    if retain_trace_seals:
        trace.update(
            {
                "moves": moves,
                "batches": batches,
                "model_sha256": model_sha256,
                "initial_assignment_sha256": _canonical_digest(
                    initial_assignment
                ),
                "final_cluster_assignment_sha256": _canonical_digest(
                    cluster_assignment
                ),
            }
        )
    return final, trace


def validate_partition_pressure_native_against_exhaustive(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
) -> Dict[str, Any]:
    expected_assignment, expected_trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        initial_assignment,
        max_moves=len(native_trace.get("moves", [])),
    )
    if (
        native_assignment.get("cluster_assignment")
        != expected_assignment.get("cluster_assignment")
    ):
        raise ValidationError("native PATRON assignment differs from exhaustive")
    if len(native_trace["moves"]) != len(expected_trace["moves"]):
        raise ValidationError("native PATRON move count differs from exhaustive")
    maximum_error = 0.0
    for native, expected in zip(
        native_trace["moves"], expected_trace["moves"]
    ):
        for field in (
            "index",
            "kind",
            "phase",
            "sweep",
            "cluster",
            "source",
            "target",
            "partner",
            "partner_source",
            "partner_target",
        ):
            if native[field] != expected[field]:
                raise ValidationError(
                    f"native PATRON move {native['index']} {field} mismatch"
                )
        if native["ranked_objective_key"] != expected["ranked_objective_key"]:
            raise ValidationError(
                f"native PATRON move {native['index']} rank mismatch"
            )
        for native_key, expected_key in (
            (native["before_objective_key"], expected["before_objective_key"]),
            (native["after_objective_key"], expected["after_objective_key"]),
        ):
            maximum_error = max(
                maximum_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(native_key, expected_key)
                ),
            )
    if maximum_error > 1.0e-10:
        raise ValidationError("native PATRON raw objective mismatch")
    return {
        "status": "pass",
        "moves": len(expected_trace["moves"]),
        "maximum_raw_objective_error": maximum_error,
    }


def validate_partition_pressure_native_bundle(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    *,
    timing_database_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate a production PATRON bundle without rerunning its heuristic.

    Compact exact mode is reconstructed move-for-move.  Scalable mode is a
    deterministic heuristic rather than a global-optimality claim; its gate
    independently rebuilds the immutable model, complete initial/final
    objectives, legal assignment, critical multipass order, and every recorded
    assignment transition in linear or near-linear work.
    """

    model_validation = validate_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
        model,
        timing_database_path=timing_database_path,
    )
    trace_schema = native_trace.get("schema")
    if (
        trace_schema
        not in (
            PARTITION_PRESSURE_TRACE_SCHEMA,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V14,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7,
        )
        or native_trace.get("provider")
        not in (
            PARTITION_PRESSURE_NATIVE_PROVIDER,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V14,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7,
        )
        or native_trace.get("model_sha256") != _canonical_digest(model)
        or native_trace.get("initial_assignment_sha256")
        != _canonical_digest(initial_assignment)
    ):
        raise ValidationError("native PATRON trace seal is invalid")
    validate_partition_artifacts(
        ir, platform, clusters_artifact, initial_assignment
    )
    validate_partition_artifacts(
        ir, platform, clusters_artifact, native_assignment
    )
    if native_trace.get("final_cluster_assignment_sha256") != (
        _canonical_digest(native_assignment.get("cluster_assignment"))
    ):
        raise ValidationError("native PATRON final assignment seal is invalid")
    mode = native_trace.get("mode")
    if mode == "endpoint-exact-global-best-v6":
        if (
            trace_schema != PARTITION_PRESSURE_TRACE_SCHEMA
            or native_trace.get("provider")
            != PARTITION_PRESSURE_NATIVE_PROVIDER
        ):
            raise ValidationError("native PATRON exact trace schema is invalid")
        exact = validate_partition_pressure_native_against_exhaustive(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            initial_assignment,
            native_assignment,
            native_trace,
        )
        return {
            "status": "pass",
            "mode": mode,
            "qualification": "move-for-move-exhaustive",
            "model_validation": model_validation,
            **exact,
        }
    if mode not in (
        "endpoint-exact-critical-ejection-v6",
        "endpoint-exact-critical-flow-v7",
        "endpoint-exact-critical-flow-v8",
        "endpoint-exact-critical-flow-v9",
        "endpoint-exact-critical-flow-v10",
        "endpoint-exact-critical-flow-v11",
        "endpoint-exact-critical-flow-v14",
    ):
        raise ValidationError("native PATRON trace mode is invalid")
    expected_provider = (
        {
            "endpoint-exact-critical-flow-v7": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7
            ),
            "endpoint-exact-critical-flow-v8": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8
            ),
            "endpoint-exact-critical-flow-v9": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9
            ),
            "endpoint-exact-critical-flow-v10": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER
            ),
            "endpoint-exact-critical-flow-v11": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11
            ),
            "endpoint-exact-critical-flow-v14": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V14
            ),
        }[mode]
        if mode in (
            "endpoint-exact-critical-flow-v7",
            "endpoint-exact-critical-flow-v8",
            "endpoint-exact-critical-flow-v9",
            "endpoint-exact-critical-flow-v10",
            "endpoint-exact-critical-flow-v11",
            "endpoint-exact-critical-flow-v14",
        )
        else PARTITION_PRESSURE_NATIVE_PROVIDER
    )
    if native_trace.get("provider") != expected_provider:
        raise ValidationError("native PATRON trace provider is invalid")

    moves = native_trace.get("moves")
    batches = native_trace.get("batches", [])
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    max_sweeps = native_trace.get("configuration", {}).get(
        "max_scalable_sweeps"
    )
    ejection_critical_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_critical_limit"
    )
    ejection_donor_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_donor_limit"
    )
    max_ejections = native_trace.get("configuration", {}).get(
        "max_scalable_ejections"
    )
    flow_configuration = native_trace.get("configuration", {}).get(
        "flow_refinement"
    )
    flow_enabled = mode in (
        "endpoint-exact-critical-flow-v7",
        "endpoint-exact-critical-flow-v8",
        "endpoint-exact-critical-flow-v9",
        "endpoint-exact-critical-flow-v10",
        "endpoint-exact-critical-flow-v11",
        "endpoint-exact-critical-flow-v14",
    )
    flow_version = {
        "endpoint-exact-critical-flow-v7": 1,
        "endpoint-exact-critical-flow-v8": 2,
        "endpoint-exact-critical-flow-v9": 3,
        "endpoint-exact-critical-flow-v10": 4,
        "endpoint-exact-critical-flow-v11": 5,
        "endpoint-exact-critical-flow-v14": 8,
    }.get(mode, 1)
    expected_algorithm_version = (
        {
            1: 7,
            2: 8,
            3: 9,
            4: 10,
            5: 11,
            8: 14,
        }[flow_version]
        if flow_enabled
        else 6
    )
    recorded_algorithm_version = native_trace.get(
        "configuration", {}
    ).get("algorithm_version", expected_algorithm_version)
    if recorded_algorithm_version != expected_algorithm_version:
        raise ValidationError(
            "native PATRON algorithm-version certificate is invalid"
        )
    if flow_version == 5:
        if physical_feedback is None:
            raise ValidationError(
                "native PATRON v11 lacks physical feedback evidence"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        expected_feedback_sha256 = _canonical_digest(physical_feedback)
        if native_trace.get("physical_feedback_sha256") != (
            expected_feedback_sha256
        ):
            raise ValidationError(
                "native PATRON physical feedback seal is invalid"
            )
    else:
        if physical_feedback is not None or physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON trace has unexpected physical feedback"
            )
        expected_feedback_sha256 = None
    expected_flow_configuration = _flow_refinement_configuration(
        flow_enabled,
        len(model["clusters"]),
        version=flow_version,
        physical_feedback_sha256=expected_feedback_sha256,
        physical_feedback_scale=(
            physical_feedback_scale if flow_version == 5 else 0.0
        ),
    )
    if flow_configuration is None and not flow_enabled:
        # V6 artifacts predate the explicit disabled-flow certificate.
        flow_configuration = expected_flow_configuration
    if (
        not isinstance(moves, list)
        or not isinstance(batches, list)
        or (
            mode
            in (
                "endpoint-exact-critical-flow-v7",
                "endpoint-exact-critical-flow-v8",
                "endpoint-exact-critical-flow-v9",
                "endpoint-exact-critical-flow-v10",
                "endpoint-exact-critical-flow-v11",
                "endpoint-exact-critical-flow-v14",
            )
            and (
                trace_schema
                != (
                    {
                        1: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7,
                        2: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8,
                        3: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
                        4: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
                        5: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
                        8: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V14,
                    }[flow_version]
                )
            )
        )
        or (
            mode == "endpoint-exact-critical-ejection-v6"
            and (
                trace_schema != PARTITION_PRESSURE_TRACE_SCHEMA
                or batches
            )
        )
        or isinstance(max_moves, bool)
        or not isinstance(max_moves, int)
        or max_moves < 0
        or len(moves) > max_moves
        or isinstance(max_sweeps, bool)
        or not isinstance(max_sweeps, int)
        or max_sweeps <= 0
        or max_sweeps
        != model["configuration"].get("max_scalable_sweeps")
        or isinstance(ejection_critical_limit, bool)
        or not isinstance(ejection_critical_limit, int)
        or ejection_critical_limit < 0
        or ejection_critical_limit
        != model["configuration"].get("scalable_ejection_critical_limit")
        or isinstance(ejection_donor_limit, bool)
        or not isinstance(ejection_donor_limit, int)
        or ejection_donor_limit < 0
        or ejection_donor_limit
        != model["configuration"].get("scalable_ejection_donor_limit")
        or isinstance(max_ejections, bool)
        or not isinstance(max_ejections, int)
        or max_ejections < 0
        or max_ejections
        != model["configuration"].get("max_scalable_ejections")
        or flow_configuration != expected_flow_configuration
    ):
        raise ValidationError("native PATRON scalable trace bounds are invalid")
    physical_hop_guard_scale_ns = float(
        expected_flow_configuration.get("physical_hop_guard_scale_ns", 0.0)
    )
    cluster_ids = {
        record["cluster"] for record in model["clusters"]
    }
    fpga_ids = {record["fpga"] for record in model["fpgas"]}
    fixed = {
        record["cluster"]: record["fixed_fpga"]
        for record in model["clusters"]
    }
    exposure = {cluster: 0.0 for cluster in cluster_ids}
    net_by_id = {record["net"]: record for record in model["nets"]}
    for path in model["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - path["base_slack_ns"] / path["clock_period_ns"],
            ),
        )
        weight = 1.0 + 9.0 * criticality * criticality
        for cluster in {
            cluster
            for net in path["path_nets"]
            if net in net_by_id
            for cluster in (
                net_by_id[net]["drivers"] + net_by_id[net]["sinks"]
            )
        }:
            exposure[cluster] += weight
    sweep_order = sorted(
        cluster_ids, key=lambda cluster: (-exposure[cluster], cluster)
    )
    sweep_index = {cluster: index for index, cluster in enumerate(sweep_order)}
    current = dict(initial_assignment["cluster_assignment"])
    initial_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
        include_tdm_wait=True,
        physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        physical_feedback=physical_feedback,
        physical_feedback_scale=physical_feedback_scale,
        path_transition_objective=(flow_version == 8),
    )
    previous_rank = _ranked_key(initial_evaluation)
    previous_sweep = -1
    previous_sweep_index = -1
    previous_phase = 0
    previous_ejection_index = -1
    ejection_count = 0
    previous_after = None
    maximum_chain_error = 0.0
    for index, move in enumerate(moves):
        if not isinstance(move, dict) or move.get("index") != index:
            raise ValidationError("native PATRON move index is invalid")
        cluster = move.get("cluster")
        phase = move.get("phase")
        kind = move.get("kind")
        sweep = move.get("sweep")
        source = move.get("source")
        target = move.get("target")
        partner = move.get("partner")
        partner_source = move.get("partner_source")
        partner_target = move.get("partner_target")
        common_invalid = (
            isinstance(phase, bool)
            or not isinstance(phase, int)
            or phase not in (0, 1)
            or kind != ("move" if phase == 0 else "ejection")
            or isinstance(sweep, bool)
            or not isinstance(sweep, int)
            or cluster not in cluster_ids
            or source not in fpga_ids
            or target not in fpga_ids
            or source == target
            or fixed[cluster] is not None
            or current.get(cluster) != source
            or phase < previous_phase
        )
        direct_invalid = phase == 0 and not common_invalid and (
            sweep < 0
            or sweep >= max_sweeps
            or sweep > previous_sweep + 1
            or (previous_sweep < 0 and sweep != 0)
            or (
                sweep == previous_sweep
                and sweep_index.get(cluster, -1) <= previous_sweep_index
            )
            or partner is not None
            or partner_source is not None
            or partner_target is not None
        )
        ejection_invalid = phase == 1 and not common_invalid and (
            sweep != 0
            or partner not in cluster_ids
            or partner == cluster
            or partner_source not in fpga_ids
            or partner_target not in fpga_ids
            or fixed.get(partner) is not None
            or current.get(partner) != partner_source
            or target != partner_source
            or partner_target == partner_source
            or sweep_index.get(cluster, ejection_critical_limit)
            >= ejection_critical_limit
            or sweep_index.get(cluster, -1) <= previous_ejection_index
            or ejection_count >= max_ejections
        )
        if common_invalid or direct_invalid or ejection_invalid:
            raise ValidationError(
                f"native PATRON move {index} transition is invalid"
            )
        before = move.get("before_objective_key")
        after = move.get("after_objective_key")
        ranked = move.get("ranked_objective_key")
        if (
            not isinstance(before, list)
            or not isinstance(after, list)
            or not isinstance(ranked, list)
            or len(before) != 8
            or len(after) != 8
            or len(ranked) != 10
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in before + after
            )
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in ranked
            )
            or ranked[0] < 0
            or ranked[1] < 0
            or tuple(ranked) >= previous_rank
        ):
            raise ValidationError(
                f"native PATRON move {index} objective is invalid"
            )
        expected_objective_rank = [
            round(float(value) / GAIN_QUANTUM)
            if objective_index < 2
            else round(float(value))
            for objective_index, value in enumerate(after)
        ]
        if ranked[2:] != expected_objective_rank:
            raise ValidationError(
                f"native PATRON move {index} ranked objective is invalid"
            )
        if previous_after is not None:
            maximum_chain_error = max(
                maximum_chain_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(before, previous_after)
                ),
            )
        current[cluster] = target
        if phase == 1:
            current[partner] = partner_target
            previous_ejection_index = sweep_index[cluster]
            ejection_count += 1
        previous_after = after
        previous_rank = tuple(ranked)
        if phase == 0:
            if sweep != previous_sweep:
                previous_sweep_index = -1
            previous_sweep = sweep
            previous_sweep_index = sweep_index[cluster]
        previous_phase = phase
    for batch_index, batch in enumerate(batches):
        if (
            not isinstance(batch, dict)
            or batch.get("index") != batch_index
            or not isinstance(batch.get("changes"), list)
            or not batch["changes"]
        ):
            raise ValidationError("native PATRON batch record is invalid")
        before_evaluation = evaluate_partition_pressure(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            current,
            include_tdm_wait=True,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            physical_feedback=physical_feedback,
            physical_feedback_scale=physical_feedback_scale,
            path_transition_objective=(flow_version == 8),
        )
        before_expected = before_evaluation["metrics"]["objective_key"]
        trial = dict(current)
        changed = set()
        previous_cluster = None
        for change in batch["changes"]:
            if not isinstance(change, dict):
                raise ValidationError("native PATRON batch change is invalid")
            cluster = change.get("cluster")
            source = change.get("source")
            target = change.get("target")
            if (
                cluster not in cluster_ids
                or source not in fpga_ids
                or target not in fpga_ids
                or source == target
                or fixed.get(cluster) is not None
                or current.get(cluster) != source
                or cluster in changed
                or (
                    previous_cluster is not None
                    and cluster <= previous_cluster
                )
            ):
                raise ValidationError(
                    "native PATRON batch transition is invalid"
                )
            changed.add(cluster)
            previous_cluster = cluster
            trial[cluster] = target
        after_evaluation = evaluate_partition_pressure(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            trial,
            include_tdm_wait=True,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            physical_feedback=physical_feedback,
            physical_feedback_scale=physical_feedback_scale,
            path_transition_objective=(flow_version == 8),
        )
        after_expected = after_evaluation["metrics"]["objective_key"]
        before = batch.get("before_objective_key")
        after = batch.get("after_objective_key")
        ranked = batch.get("ranked_objective_key")
        if (
            not isinstance(before, list)
            or not isinstance(after, list)
            or not isinstance(ranked, list)
            or len(before) != 8
            or len(after) != 8
            or len(ranked) != 10
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in before + after
            )
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in ranked
            )
        ):
            raise ValidationError("native PATRON batch objective is invalid")
        for label, actual, expected in (
            ("before", before, before_expected),
            ("after", after, after_expected),
        ):
            for objective_index, (left, right) in enumerate(
                zip(actual, expected)
            ):
                matches = (
                    math.isclose(
                        float(left),
                        float(right),
                        rel_tol=1.0e-12,
                        abs_tol=1.0e-9,
                    )
                    if objective_index < 2
                    else float(left) == float(right)
                )
                if not matches:
                    raise ValidationError(
                        f"native PATRON batch {label} objective mismatch"
                    )
        expected_rank = list(_ranked_key(after_evaluation))
        before_rank = _ranked_key(before_evaluation)
        if (
            ranked != expected_rank
            or tuple(ranked) >= before_rank
        ):
            raise ValidationError(
                "native PATRON batch is not lexicographically improving"
            )
        if previous_after is not None:
            maximum_chain_error = max(
                maximum_chain_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(before, previous_after)
                ),
            )
        previous_after = after
        current = trial
    if current != native_assignment["cluster_assignment"]:
        raise ValidationError("native PATRON move chain assignment mismatch")
    if maximum_chain_error > 1.0e-9:
        raise ValidationError("native PATRON objective chain is discontinuous")

    final_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        native_assignment["cluster_assignment"],
        include_tdm_wait=True,
        physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        physical_feedback=physical_feedback,
        physical_feedback_scale=physical_feedback_scale,
        path_transition_objective=(flow_version == 8),
    )
    maximum_endpoint_error = 0.0
    maximum_endpoint_relative_error = 0.0
    for actual, expected in (
        (
            native_trace.get("initial_metrics", {}).get("objective_key"),
            initial_evaluation["metrics"]["objective_key"],
        ),
        (
            native_trace.get("final_metrics", {}).get("objective_key"),
            final_evaluation["metrics"]["objective_key"],
        ),
    ):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValidationError(
                "native PATRON endpoint objective is invalid"
            )
        for objective_index, (left, right) in enumerate(
            zip(actual, expected)
        ):
            left_value = float(left)
            right_value = float(right)
            error = abs(left_value - right_value)
            maximum_endpoint_error = max(maximum_endpoint_error, error)
            maximum_endpoint_relative_error = max(
                maximum_endpoint_relative_error,
                error / max(1.0, abs(right_value)),
            )
            if objective_index < 2:
                matches = math.isclose(
                    left_value,
                    right_value,
                    rel_tol=1.0e-12,
                    abs_tol=1.0e-9,
                )
            else:
                matches = left_value == right_value
            if not matches:
                raise ValidationError(
                    "native PATRON endpoint objective mismatch: "
                    f"field={objective_index}, native={left_value}, "
                    f"independent={right_value}, error={error}"
                )
    return {
        "status": "pass",
        "mode": mode,
        "qualification": "linear-transition-and-endpoint-reconstruction",
        "model_validation": model_validation,
        "moves": len(moves),
        "batches": len(batches),
        "maximum_objective_chain_error": maximum_chain_error,
        "maximum_endpoint_objective_error": maximum_endpoint_error,
        "maximum_endpoint_relative_objective_error": (
            maximum_endpoint_relative_error
        ),
    }


def validate_partition_pressure_scalable_trace(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
) -> Dict[str, Any]:
    """Independently replay the scalable criticality-ordered sweep.

    This exhaustive reconstruction is intended for compact/scaled-down
    instances.  Large production bundles use the indexed certificate checker.
    """

    if native_trace.get("mode") != "endpoint-exact-critical-ejection-v6":
        raise ValidationError("native PATRON scalable trace mode is invalid")
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    if isinstance(max_moves, bool) or not isinstance(max_moves, int) or max_moves < 0:
        raise ValidationError("native PATRON scalable max_moves is invalid")
    max_sweeps = native_trace.get("configuration", {}).get(
        "max_scalable_sweeps"
    )
    ejection_critical_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_critical_limit"
    )
    ejection_donor_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_donor_limit"
    )
    max_ejections = native_trace.get("configuration", {}).get(
        "max_scalable_ejections"
    )
    if (
        isinstance(max_sweeps, bool)
        or not isinstance(max_sweeps, int)
        or max_sweeps <= 0
        or max_sweeps
        != model["configuration"].get("max_scalable_sweeps")
        or isinstance(ejection_critical_limit, bool)
        or not isinstance(ejection_critical_limit, int)
        or ejection_critical_limit < 0
        or ejection_critical_limit
        != model["configuration"].get("scalable_ejection_critical_limit")
        or isinstance(ejection_donor_limit, bool)
        or not isinstance(ejection_donor_limit, int)
        or ejection_donor_limit < 0
        or ejection_donor_limit
        != model["configuration"].get("scalable_ejection_donor_limit")
        or isinstance(max_ejections, bool)
        or not isinstance(max_ejections, int)
        or max_ejections < 0
        or max_ejections
        != model["configuration"].get("max_scalable_ejections")
    ):
        raise ValidationError("native PATRON scalable sweep limit is invalid")
    clusters = sorted(record["cluster"] for record in model["clusters"])
    parts = sorted(record["fpga"] for record in model["fpgas"])
    exposure = {cluster: 0.0 for cluster in clusters}
    net_by_id = {record["net"]: record for record in model["nets"]}
    for path in model["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - path["base_slack_ns"] / path["clock_period_ns"],
            ),
        )
        weight = 1.0 + 9.0 * criticality * criticality
        touched = {
            cluster
            for net in path["path_nets"]
            if net in net_by_id
            for cluster in (
                net_by_id[net]["drivers"] + net_by_id[net]["sinks"]
            )
        }
        for cluster in touched:
            exposure[cluster] += weight
    order = sorted(clusters, key=lambda cluster: (-exposure[cluster], cluster))
    fixed = {
        record["cluster"]: record["fixed_fpga"]
        for record in model["clusters"]
    }
    current = dict(initial_assignment["cluster_assignment"])
    current_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
        include_tdm_wait=True,
    )
    moves = native_trace.get("moves")
    if not isinstance(moves, list):
        raise ValidationError("native PATRON scalable moves are invalid")
    move_index = 0
    maximum_error = 0.0
    for _sweep in range(max_sweeps):
        sweep_start = move_index
        for cluster in order:
            if move_index >= max_moves:
                break
            candidates = []
            source = current[cluster]
            for target in parts:
                if target == source:
                    continue
                trial = dict(current)
                trial[cluster] = target
                try:
                    evaluation = evaluate_partition_pressure(
                        ir,
                        platform,
                        clusters_artifact,
                        constraints,
                        route_constraints,
                        model,
                        trial,
                        include_tdm_wait=True,
                    )
                except ValidationError:
                    continue
                ranked = _ranked_key(evaluation)
                if ranked < _ranked_key(current_evaluation):
                    candidates.append((ranked, target, evaluation))
            if not candidates:
                continue
            ranked, target, selected = min(
                candidates, key=lambda item: (item[0], item[1])
            )
            if move_index >= len(moves):
                raise ValidationError(
                    "native PATRON scalable trace omitted a move"
                )
            actual = moves[move_index]
            expected_identity = {
                "index": move_index,
                "kind": "move",
                "phase": 0,
                "sweep": _sweep,
                "cluster": cluster,
                "source": source,
                "target": target,
                "partner": None,
                "partner_source": None,
                "partner_target": None,
            }
            for field, expected in expected_identity.items():
                if actual.get(field) != expected:
                    raise ValidationError(
                        f"native PATRON scalable move {move_index} "
                        f"{field} mismatch"
                    )
            if actual.get("ranked_objective_key") != list(ranked):
                raise ValidationError(
                    f"native PATRON scalable move {move_index} rank mismatch"
                )
            for actual_key, expected_key in (
                (
                    actual["before_objective_key"],
                    current_evaluation["metrics"]["objective_key"],
                ),
                (
                    actual["after_objective_key"],
                    selected["metrics"]["objective_key"],
                ),
            ):
                maximum_error = max(
                    maximum_error,
                    max(
                        abs(float(left) - float(right))
                        for left, right in zip(actual_key, expected_key)
                    ),
                )
            current[cluster] = target
            current_evaluation = selected
            move_index += 1
        if move_index >= max_moves or move_index == sweep_start:
            break

    donors = {part: [] for part in parts}
    for cluster in sorted(clusters, key=lambda item: (exposure[item], item)):
        if fixed[cluster] is None:
            donors[current[cluster]].append(cluster)
    accepted_ejections = 0
    for cluster in order[:ejection_critical_limit]:
        if (
            accepted_ejections >= max_ejections
            or move_index >= max_moves
        ):
            break
        if fixed[cluster] is not None:
            continue
        source = current[cluster]
        candidates = []
        for target in parts:
            if target == source:
                continue
            considered = 0
            for partner in donors[target]:
                if considered >= ejection_donor_limit:
                    break
                if (
                    partner == cluster
                    or current[partner] != target
                    or fixed[partner] is not None
                ):
                    continue
                considered += 1
                for partner_target in parts:
                    if partner_target == target:
                        continue
                    trial = dict(current)
                    trial[cluster] = target
                    trial[partner] = partner_target
                    try:
                        evaluation = evaluate_partition_pressure(
                            ir,
                            platform,
                            clusters_artifact,
                            constraints,
                            route_constraints,
                            model,
                            trial,
                            include_tdm_wait=True,
                        )
                    except ValidationError:
                        continue
                    ranked = _ranked_key(evaluation)
                    if ranked < _ranked_key(current_evaluation):
                        candidates.append(
                            (
                                ranked,
                                target,
                                partner,
                                partner_target,
                                evaluation,
                            )
                        )
        if not candidates:
            continue
        ranked, target, partner, partner_target, selected = min(
            candidates,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
        if move_index >= len(moves):
            raise ValidationError(
                "native PATRON scalable trace omitted an ejection"
            )
        actual = moves[move_index]
        expected_identity = {
            "index": move_index,
            "kind": "ejection",
            "phase": 1,
            "sweep": 0,
            "cluster": cluster,
            "source": source,
            "target": target,
            "partner": partner,
            "partner_source": target,
            "partner_target": partner_target,
        }
        for field, expected in expected_identity.items():
            if actual.get(field) != expected:
                raise ValidationError(
                    f"native PATRON scalable ejection {move_index} "
                    f"{field} mismatch"
                )
        if actual.get("ranked_objective_key") != list(ranked):
            raise ValidationError(
                f"native PATRON scalable ejection {move_index} rank mismatch"
            )
        for actual_key, expected_key in (
            (
                actual["before_objective_key"],
                current_evaluation["metrics"]["objective_key"],
            ),
            (
                actual["after_objective_key"],
                selected["metrics"]["objective_key"],
            ),
        ):
            maximum_error = max(
                maximum_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(actual_key, expected_key)
                ),
            )
        current[cluster] = target
        current[partner] = partner_target
        current_evaluation = selected
        move_index += 1
        accepted_ejections += 1
    if move_index != len(moves):
        raise ValidationError("native PATRON scalable trace has extra moves")
    if current != native_assignment.get("cluster_assignment"):
        raise ValidationError("native PATRON scalable final assignment mismatch")
    if maximum_error > 1.0e-9:
        raise ValidationError("native PATRON scalable raw objective mismatch")
    return {
        "status": "pass",
        "moves": move_index,
        "maximum_raw_objective_error": maximum_error,
    }
