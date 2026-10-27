"""Source-bound post-route timing for original paths local to one FPGA."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import Any, Dict, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.timing.local_path_timing_storage import LOCAL_PATH_TIMING_STORAGE_SCHEMA, read_local_path_timing_storage


LOCAL_PATH_IDENTITY_SCHEMA = "emuflow.local-path-identity/v1"
LOCAL_PATH_IDENTITY_SCHEMA_V2 = "emuflow.local-path-identity/v2"
LOCAL_PATH_TIMING_SCHEMA = "emuflow.local-path-timing/v1"
LOCAL_PATH_QUERY_HEADER_V2 = (
    "endpoint\tkind\tstart_pin\tend_pin\tpath_pins"
)
LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION = (
    "typed-local-path-query-identity-owner-v1"
)


def local_path_timing_header(
    identity: Mapping[str, Any], *, provider: str, qualification: str
) -> Dict[str, Any]:
    """Return the bounded header shared by local-path timing stores."""

    return {
        "schema": LOCAL_PATH_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "fpga": identity["fpga"],
        "provider": provider,
        "qualification": qualification,
        "source": identity["source"],
        "identity_schema": identity["schema"],
        "coverage": identity["coverage"],
    }


class _WithoutDelayPath(Mapping[str, Any]):
    """Expose one timing record as its identity without copying its payload."""

    def __init__(self, source: Mapping[str, Any]):
        self.source = source

    def __getitem__(self, key: str) -> Any:
        if key == "delay_ns":
            raise KeyError(key)
        return self.source[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key in self.source if key != "delay_ns")

    def __len__(self) -> int:
        return len(self.source) - ("delay_ns" in self.source)


class _WithoutDelayPaths(Sequence[Mapping[str, Any]]):
    """Repeatable identity projection over list- or store-backed timing rows."""

    def __init__(self, source: Sequence[Mapping[str, Any]]):
        self.source = source

    def __len__(self) -> int:
        return len(self.source)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        item = self.source[index]
        if not isinstance(item, Mapping):
            raise ValidationError("local path timing record is invalid")
        return _WithoutDelayPath(item)

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        for item in self.source:
            if not isinstance(item, Mapping):
                raise ValidationError("local path timing record is invalid")
            yield _WithoutDelayPath(item)


class _StoredLocalPathIdentity(Mapping[str, Any]):
    """Logical identity projection over a leased typed timing owner."""

    _KEYS = (
        "schema", "status", "design", "fpga", "provider", "source",
        "coverage", "paths",
    )

    def __init__(self, timing: Any) -> None:
        self.timing = timing
        self._paths = _WithoutDelayPaths(timing["paths"])

    @property
    def store_path(self):
        return self.timing.store_path

    def bound_store_sources(self):
        return self.timing.bound_store_sources()

    def validate_identity(self) -> Dict[str, Any]:
        validation = self.timing.validate_timing()
        if (
            self.timing["qualification"]
            != LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
            or self.timing["identity_schema"]
            != LOCAL_PATH_IDENTITY_SCHEMA_V2
            or validation["maximum_delay_ns"] != 0.0
        ):
            raise ValidationError(
                "typed local path identity owner is invalid"
            )
        return {
            "status": "pass",
            "fpga": self.timing["fpga"],
            "local_paths": validation["local_paths"],
        }

    def close(self) -> None:
        self.timing.close()

    def __enter__(self) -> "_StoredLocalPathIdentity":
        self.validate_identity()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __getitem__(self, key: str) -> Any:
        if key == "schema":
            return self.timing["identity_schema"]
        if key == "paths":
            return self._paths
        if key in self._KEYS:
            return self.timing[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)


def read_local_path_identity(path: Path) -> Mapping[str, Any]:
    """Open the current source-bound typed identity projection."""
    timing = read_local_path_timing_storage(path)
    identity = _StoredLocalPathIdentity(timing)
    try:
        identity.validate_identity()
    except BaseException:
        identity.close()
        raise
    return identity


def path_id_set_sha256(path_ids: list[str] | set[str]) -> str:
    """Return an unambiguous digest of a sorted set of original path IDs."""
    encoded = json.dumps(
        sorted(path_ids), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_local_path_identity(database: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(database, _StoredLocalPathIdentity):
        raise ValidationError("local path identity requires a current typed owner")
    return database.validate_identity()


# Native producers own preparation, ownership partitioning and query publication.
from emuflow.timing.local_path_query_native import LocalPathQueryInputs, prepare_local_path_query_inputs, write_xilinx_local_path_query


def validate_local_path_timing(database: Mapping[str, Any]) -> Dict[str, Any]:
    from emuflow.timing.local_path_timing_storage import LocalPathTimingView
    if not isinstance(database, LocalPathTimingView):
        raise ValidationError("local path timing requires a current typed owner")
    return database.validate_timing()
