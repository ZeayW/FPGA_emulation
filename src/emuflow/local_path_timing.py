"""Source-bound post-route timing for original paths local to one FPGA."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .io import read_json
from .local_path_timing_storage import (
    LOCAL_PATH_TIMING_STORAGE_SCHEMA,
    read_local_path_timing_storage,
)


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
    """Open a typed production identity or an explicit legacy JSON value."""

    raw = read_json(path)
    if not isinstance(raw, Mapping):
        raise ValidationError("local path identity root is invalid")
    if raw.get("schema") != LOCAL_PATH_TIMING_STORAGE_SCHEMA:
        return raw
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
    indexed_validator = getattr(database, "validate_identity", None)
    if callable(indexed_validator):
        return indexed_validator()
    schema = database.get("schema")
    if schema not in {
        LOCAL_PATH_IDENTITY_SCHEMA,
        LOCAL_PATH_IDENTITY_SCHEMA_V2,
    } or database.get("status") != "pass":
        raise ValidationError("local path identity is invalid")
    fpga = database.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("local path identity FPGA is invalid")
    source = database.get("source")
    if not isinstance(source, Mapping) or set(source) != {
        "path_database_sha256",
        "original_ir_sha256",
        "assignment_sha256",
        "routes_sha256",
        "original_paths",
        "original_path_ids_sha256",
    }:
        raise ValidationError("local path identity source seal is invalid")
    if (
        any(
            not isinstance(source[field], str) or len(source[field]) != 64
            for field in (
                "path_database_sha256",
                "original_ir_sha256",
                "assignment_sha256",
                "routes_sha256",
                "original_path_ids_sha256",
            )
        )
        or isinstance(source["original_paths"], bool)
        or not isinstance(source["original_paths"], int)
        or source["original_paths"] <= 0
    ):
        raise ValidationError("local path identity source seal is invalid")
    paths = database.get("paths")
    if not isinstance(paths, Sequence) or isinstance(
        paths, (str, bytes, bytearray)
    ):
        raise ValidationError("local path identity paths are invalid")
    ids = set()
    for index, path in enumerate(paths):
        context = f"local path identity[{index}]"
        expected_fields = {
            "id",
            "kind",
            "fpga",
            "clock_domain",
            "clock_period_ns",
            "start_pin",
            "end_pin",
        }
        if schema == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            expected_fields.update(
                {"required_time_ns", "measurement", "path_pins"}
            )
        if not isinstance(path, Mapping) or set(path) != expected_fields:
            raise ValidationError(f"{context} is invalid")
        path_id = path["id"]
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in ids
            or path["kind"] != "local"
            or path["fpga"] != fpga
            or not isinstance(path["clock_domain"], str)
            or not path["clock_domain"]
            or isinstance(path["clock_period_ns"], bool)
            or not isinstance(path["clock_period_ns"], (int, float))
            or not math.isfinite(float(path["clock_period_ns"]))
            or float(path["clock_period_ns"]) <= 0.0
            or not isinstance(path["start_pin"], str)
            or not path["start_pin"]
            or not isinstance(path["end_pin"], str)
            or not path["end_pin"]
        ):
            raise ValidationError(f"{context} is invalid")
        if schema == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            required_time = path["required_time_ns"]
            if (
                isinstance(required_time, bool)
                or not isinstance(required_time, (int, float))
                or not math.isfinite(float(required_time))
                or float(required_time) <= 0.0
            ):
                raise ValidationError(f"{context} required time is invalid")
            measurement = path["measurement"]
            pins = path["path_pins"]
            if measurement not in {
                "explicit-routed-path-chain",
                "endpoint-longest-path-fallback",
            } or not isinstance(pins, Sequence) or isinstance(
                pins, (str, bytes, bytearray)
            ):
                raise ValidationError(f"{context} measurement is invalid")
            if measurement == "explicit-routed-path-chain":
                if (
                    len(pins) < 2
                    or pins[0] != path["start_pin"]
                    or pins[-1] != path["end_pin"]
                    or not all(
                        isinstance(pin, str) and pin and "," not in pin
                        for pin in pins
                    )
                ):
                    raise ValidationError(f"{context} path chain is invalid")
            elif pins:
                raise ValidationError(f"{context} fallback chain is invalid")
        ids.add(path_id)
    coverage = database.get("coverage")
    if not isinstance(coverage, Mapping) or coverage != {
        "local_paths": len(paths)
    }:
        raise ValidationError("local path identity coverage is invalid")
    return {"status": "pass", "fpga": fpga, "local_paths": len(paths)}


# Native producers own preparation, ownership partitioning and query publication.
from .local_path_query_native import (LocalPathQueryInputs,
    prepare_local_path_query_inputs, write_xilinx_local_path_query)


def validate_local_path_timing(database: Mapping[str, Any]) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_timing", None)
    if callable(indexed_validator):
        return indexed_validator()
    if database.get("schema") != LOCAL_PATH_TIMING_SCHEMA:
        raise ValidationError("local path timing schema is invalid")
    paths = database.get("paths")
    if not isinstance(paths, Sequence) or isinstance(
        paths, (str, bytes, bytearray)
    ):
        raise ValidationError("local path timing paths are invalid")
    identity = {
        key: database[key]
        for key in (
            "status", "design", "fpga", "source", "coverage"
        )
    }
    identity.update(
        {
            "schema": database.get(
                "identity_schema", LOCAL_PATH_IDENTITY_SCHEMA
            ),
            "provider": "local-path-timing-validation",
            "paths": _WithoutDelayPaths(paths),
        }
    )
    checked = validate_local_path_identity(identity)
    maximum = 0.0
    measurement_counts = {
        "explicit-routed-path-chain": 0,
        "endpoint-longest-path-fallback": 0,
    }
    for item in paths:
        if not isinstance(item, Mapping):
            raise ValidationError("local path timing record is invalid")
        delay = item.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("local path timing delay is invalid")
        maximum = max(maximum, float(delay))
        if database.get("identity_schema") == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            measurement = item.get("measurement")
            if measurement in measurement_counts:
                measurement_counts[measurement] += 1
    if database.get("identity_schema") != LOCAL_PATH_IDENTITY_SCHEMA_V2:
        measurement_counts = {}
    return {
        **checked,
        "maximum_delay_ns": maximum,
        "measurement_counts": measurement_counts,
    }
