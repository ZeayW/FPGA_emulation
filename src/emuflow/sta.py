"""STA extraction adapters with stable EmuIR cut-net identity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from .errors import ValidationError
from .store_publication import (
    bind_store_source,
    publish_sqlite_store,
    commit_store_selector,
    require_store_sources_unchanged,
    validate_store_destination,
)
from .io import read_json, write_json
from .ir import EmuIR
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .phase3_storage import (
    read_phase3_assignment_view,
    read_phase3_cut_timing_view,
)
from .timing_routing import STA_PATHS_SCHEMA, compress_sta_paths
from .sta_clock_budgets import (SCHEMA as STA_PATH_DATABASE_CLOCK_SCHEMA,
    FORMAT as STA_PATH_DATABASE_CLOCK_FORMAT,STORE_SCHEMA as CLOCK_STORE_SCHEMA,
    FIELDS as CLOCK_BUDGET_FIELDS,TSV_SUFFIX as CLOCK_BUDGET_TSV_SUFFIX,
    DDL as CLOCK_BUDGET_DDL,ClockBudgetCursor,parse_tsv_budget,validate_clock_budget)


STA_PATH_DATABASE_SCHEMA = "emuflow.sta-path-database/v1"
STA_PATH_DATABASE_STREAM_SCHEMA = "emuflow.sta-path-database/v2"
STA_PATH_DATABASE_INDEX_SCHEMA = "emuflow.sta-path-database/v3"
STA_PATH_DATABASE_PAYLOAD_FORMAT_V1 = "jsonl-object/v1"
STA_PATH_DATABASE_PAYLOAD_FORMAT_V2 = "jsonl-sta-path-raw/v2"
STA_PATH_DATABASE_PAYLOAD_FORMAT = "jsonl-sta-path-row/v3"
STA_PATH_DATABASE_SQLITE_FORMAT = "sqlite-sta-path/v1"
STA_PATH_DATABASE_INDEX_SCHEMAS = {STA_PATH_DATABASE_INDEX_SCHEMA,STA_PATH_DATABASE_CLOCK_SCHEMA}
STA_PATH_DATABASE_SQLITE_FORMATS = {STA_PATH_DATABASE_SQLITE_FORMAT,STA_PATH_DATABASE_CLOCK_FORMAT}
PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"
PARTITION_NET_WEIGHTS_INDEX_SCHEMA = "emuflow.partition-net-weights/v2"
PARTITION_NET_WEIGHTS_SQLITE_FORMAT = "sqlite-net-weights/v1"
STA_PATH_DATABASE_PROVIDERS = {
    "opensta-fpga-path-database-v1",
}
EMUIR_NET_MAP_HEADER = "mapped_net_hex\temuir_net_hex"
STA_PATH_DATABASE_TSV_HEADER = (
    "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\t"
    "fixed_delay_ns\tpath_nets_hex"
)
STA_PATH_DATABASE_CLOCK_TSV_HEADER=STA_PATH_DATABASE_TSV_HEADER+'\t'+CLOCK_BUDGET_TSV_SUFFIX


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sta_path_payload(
    manifest_path: Path, value: Mapping[str, Any]
) -> tuple[Path, str, int, str]:
    payloads = value.get("payloads")
    descriptor = payloads.get("paths") if isinstance(payloads, Mapping) else None
    if not isinstance(descriptor, Mapping):
        raise ValidationError("STA path database payload is invalid")
    relative = descriptor.get("path")
    digest = descriptor.get("sha256")
    records = descriptor.get("records")
    if (
        descriptor.get("format") not in {
            STA_PATH_DATABASE_PAYLOAD_FORMAT_V1,
            STA_PATH_DATABASE_PAYLOAD_FORMAT_V2,
            STA_PATH_DATABASE_PAYLOAD_FORMAT,
            STA_PATH_DATABASE_SQLITE_FORMAT,
            STA_PATH_DATABASE_CLOCK_FORMAT,
        }
        or not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(records, bool)
        or not isinstance(records, int)
        or records <= 0
    ):
        raise ValidationError("STA path database payload is invalid")
    path = manifest_path.parent / relative
    if not path.is_file():
        raise ValidationError("STA path database payload is missing")
    if (value.get('schema')==STA_PATH_DATABASE_CLOCK_SCHEMA)!=(descriptor['format']==STA_PATH_DATABASE_CLOCK_FORMAT):
        raise ValidationError('STA path database clock schema/payload mismatch')
    return path, digest, records, str(descriptor["format"])


def sta_path_database_payload_path(
    manifest_path: Path,
    manifest: Optional[Mapping[str, Any]] = None,
) -> Optional[Path]:
    """Return the external path payload owned by a TimingPathDB manifest.

    Legacy v1 fixtures own their small inline population and therefore have
    no sidecar.  Production v2/v3 manifests must retain their sealed payload
    whenever the manifest is copied, archived, or published.
    """

    value = read_json(manifest_path) if manifest is None else manifest
    if value.get("schema") == STA_PATH_DATABASE_SCHEMA:
        return None
    if value.get("schema") not in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    return _sta_path_payload(manifest_path, value)[0]


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.execute("PRAGMA query_only = ON")
    return connection


def _verify_file_digest(path: Path, expected: str, context: str) -> None:
    if _file_sha256(path) != expected:
        raise ValidationError(f"{context} digest disagrees")


def _sta_path_endpoint_row(endpoint: Mapping[str, Any]) -> list[Any]:
    return [
        endpoint.get("object"),
        endpoint.get("instance"),
        endpoint.get("port"),
        endpoint.get("bit"),
    ]


def _sta_path_record_row(record: Mapping[str, Any]) -> list[Any]:
    """Encode one path without repeating JSON object keys per endpoint."""

    if 'clock_budget'in record:
        raise ValidationError('clock-budget paths require indexed v4 storage')
    row: list[Any] = [
        record.get("id"),
        record.get("clock_domain"),
        record.get("clock_period_ns"),
        record.get("slack_ns"),
        record.get("fixed_delay_ns"),
        record.get("path_nets"),
    ]
    has_startpoint = "startpoint" in record
    has_endpoint = "endpoint" in record
    if has_startpoint != has_endpoint:
        raise ValidationError("STA path database record endpoints are invalid")
    if has_startpoint:
        startpoint = record["startpoint"]
        endpoint = record["endpoint"]
        if not isinstance(startpoint, Mapping) or not isinstance(
            endpoint, Mapping
        ):
            raise ValidationError(
                "STA path database record endpoints are invalid"
            )
        row.extend(
            [
                _sta_path_endpoint_row(startpoint),
                _sta_path_endpoint_row(endpoint),
            ]
        )
    return row


def _sta_path_record_from_row(value: Any, index: int) -> Dict[str, Any]:
    if not isinstance(value, list) or len(value) not in {6, 8}:
        raise ValidationError(
            f"STA path database payload record {index} is invalid"
        )
    record: Dict[str, Any] = {
        "id": value[0],
        "clock_domain": value[1],
        "clock_period_ns": value[2],
        "slack_ns": value[3],
        "fixed_delay_ns": value[4],
        "path_nets": value[5],
    }
    if len(value) == 8:
        endpoints = value[6:8]
        if any(
            not isinstance(endpoint, list) or len(endpoint) != 4
            for endpoint in endpoints
        ):
            raise ValidationError(
                f"STA path database payload record {index} is invalid"
            )
        for name, endpoint in zip(("startpoint", "endpoint"), endpoints):
            record[name] = {
                "object": endpoint[0],
                "instance": endpoint[1],
                "port": endpoint[2],
                "bit": endpoint[3],
            }
    return record


def iter_sta_path_database_paths(
    path: Path,
    value: Mapping[str, Any],
    *,
    verify_payload: bool = True,
) -> Iterator[Mapping[str, Any]]:
    """Iterate an inline, legacy JSONL, or indexed TimingPathDB once."""

    schema = value.get("schema")
    if schema == STA_PATH_DATABASE_SCHEMA:
        records = value.get("paths")
        if not isinstance(records, list):
            raise ValidationError("STA path database paths are invalid")
        yield from records
        return
    if schema not in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    payload_path, expected_digest, expected_records, payload_format = _sta_path_payload(
        path, value
    )
    normalization = (
        _validate_database_normalization(value.get("normalization"))
        if payload_format in {
            STA_PATH_DATABASE_PAYLOAD_FORMAT_V2,
            STA_PATH_DATABASE_PAYLOAD_FORMAT,
            STA_PATH_DATABASE_SQLITE_FORMAT,
            STA_PATH_DATABASE_CLOCK_FORMAT,
        }
        else None
    )
    if payload_format in STA_PATH_DATABASE_SQLITE_FORMATS:
        if schema not in STA_PATH_DATABASE_INDEX_SCHEMAS:
            raise ValidationError("STA path database payload/schema mismatch")
        if verify_payload:
            _verify_file_digest(
                payload_path,
                expected_digest,
                "STA path database payload",
            )
        connection = _open_sqlite_read_only(payload_path)
        try:
            budgets=ClockBudgetCursor(connection)if payload_format==STA_PATH_DATABASE_CLOCK_FORMAT else None
            cursor = connection.execute(
                """
                SELECT p.ordinal, p.path_id, p.clock_domain,
                       p.clock_period_ns, p.slack_ns, p.fixed_delay_ns,
                       p.start_object, p.start_instance, p.start_port,
                       p.start_bit, p.end_object, p.end_instance,
                       p.end_port, p.end_bit, n.net_ordinal, n.net_id
                FROM paths AS p
                JOIN path_nets AS n ON n.path_ordinal = p.ordinal
                ORDER BY p.ordinal, n.net_ordinal
                """
            )
            current_ordinal: Optional[int] = None
            record: Optional[Dict[str, Any]] = None
            count = 0
            for row in cursor:
                ordinal = int(row[0])
                if current_ordinal != ordinal:
                    if record is not None:
                        count += 1
                        yield record
                    current_ordinal = ordinal
                    record = {
                        "id": row[1],
                        "clock_domain": row[2],
                        "clock_period_ns": float(row[3]),
                        "slack_ns": float(row[4]),
                        "fixed_delay_ns": float(row[5]),
                        "path_nets": [],
                    }
                    if row[6] is not None:
                        record["startpoint"] = {
                            "object": row[6],
                            "instance": row[7],
                            "port": row[8],
                            "bit": int(row[9]),
                        }
                        record["endpoint"] = {
                            "object": row[10],
                            "instance": row[11],
                            "port": row[12],
                            "bit": int(row[13]),
                        }
                    record["normalized_slack"] = _normalized_slack(
                        float(row[3]), float(row[4]), normalization
                    )
                    if budgets is not None:budgets.attach(ordinal,record)
                if record is None or int(row[14]) != len(record["path_nets"]):
                    raise ValidationError(
                        "STA path database SQLite net ordering is invalid"
                    )
                record["path_nets"].append(row[15])
            if record is not None:
                count += 1
                yield record
            if count != expected_records:
                raise ValidationError("STA path database payload count disagrees")
            if budgets is not None:budgets.finish()
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "STA path database SQLite payload is invalid"
            ) from error
        finally:
            connection.close()
        return

    digest = hashlib.sha256()
    count = 0
    with payload_path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise ValidationError("STA path database payload is truncated")
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    f"STA path database payload record {count} is invalid"
                ) from error
            if payload_format == STA_PATH_DATABASE_PAYLOAD_FORMAT:
                record = _sta_path_record_from_row(record, count)
            count += 1
            if payload_format in {
                STA_PATH_DATABASE_PAYLOAD_FORMAT_V2,
                STA_PATH_DATABASE_PAYLOAD_FORMAT,
            }:
                if not isinstance(record, dict) or "normalized_slack" in record:
                    raise ValidationError(
                        f"STA path database payload record {count - 1} is invalid"
                    )
                record = dict(record)
                try:
                    record["normalized_slack"] = _normalized_slack(
                        float(record["clock_period_ns"]),
                        float(record["slack_ns"]),
                        normalization,
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise ValidationError(
                        f"STA path database payload record {count - 1} is invalid"
                    ) from error
            yield record
    if count != expected_records:
        raise ValidationError("STA path database payload count disagrees")
    if verify_payload and digest.hexdigest() != expected_digest:
        raise ValidationError("STA path database payload digest disagrees")


def iter_selected_sta_path_database_paths(
    path: Path,
    value: Mapping[str, Any],
    path_ids: set[str],
    *,
    verify_payload: bool = False,
) -> Iterator[Mapping[str, Any]]:
    """Read only named timing paths from an indexed TimingPathDB.

    Phase 7 logic-segment binding needs the paths named by Phase 4 routes, not
    the complete endpoint population.  Loading the whole SQLite sidecar into
    Python used several gigabytes on NVDLA.  The v3 payload already has a
    unique index on ``paths.path_id``; use it directly and retain the generic
    streaming fallback for small legacy databases.

    ``verify_payload`` defaults to false deliberately.  The producer validated
    and sealed the payload when publishing the phase boundary.  Routine
    consumers validate schema, selected-record coverage, and circuit identity;
    rereading a multi-gigabyte payload solely to repeat its hash is an explicit
    offline audit, not part of the hot path.
    """

    requested = set(path_ids)
    if not requested:
        return
    schema = value.get("schema")
    if schema in STA_PATH_DATABASE_INDEX_SCHEMAS:
        payload_path, expected_digest, _records, payload_format = (
            _sta_path_payload(path, value)
        )
        if payload_format in STA_PATH_DATABASE_SQLITE_FORMATS:
            if verify_payload:
                _verify_file_digest(
                    payload_path,
                    expected_digest,
                    "STA path database payload",
                )
            normalization = _validate_database_normalization(
                value.get("normalization")
            )
            connection = _open_sqlite_read_only(payload_path)
            found: set[str] = set()
            ordered_requested = sorted(requested)
            try:
                for offset in range(0, len(ordered_requested), 400):
                    batch = ordered_requested[offset : offset + 400]
                    budgets=ClockBudgetCursor(connection,batch)if payload_format==STA_PATH_DATABASE_CLOCK_FORMAT else None
                    placeholders = ",".join("?" for _ in batch)
                    cursor = connection.execute(
                        f"""
                        SELECT p.ordinal, p.path_id, p.clock_domain,
                               p.clock_period_ns, p.slack_ns,
                               p.fixed_delay_ns, p.start_object,
                               p.start_instance, p.start_port, p.start_bit,
                               p.end_object, p.end_instance, p.end_port,
                               p.end_bit, n.net_ordinal, n.net_id
                        FROM paths AS p JOIN path_nets AS n
                          ON n.path_ordinal = p.ordinal
                        WHERE p.path_id IN ({placeholders})
                        ORDER BY p.ordinal, n.net_ordinal
                        """,
                        batch,
                    )
                    current_ordinal: Optional[int] = None
                    record: Optional[Dict[str, Any]] = None
                    for row in cursor:
                        ordinal = int(row[0])
                        if current_ordinal != ordinal:
                            if record is not None:
                                found.add(record["id"])
                                yield record
                            current_ordinal = ordinal
                            record = {
                                "id": row[1],
                                "clock_domain": row[2],
                                "clock_period_ns": float(row[3]),
                                "slack_ns": float(row[4]),
                                "fixed_delay_ns": float(row[5]),
                                "path_nets": [],
                                "normalized_slack": _normalized_slack(
                                    float(row[3]),
                                    float(row[4]),
                                    normalization,
                                ),
                            }
                            if row[6] is not None:
                                record["startpoint"] = {
                                    "object": row[6],
                                    "instance": row[7],
                                    "port": row[8],
                                    "bit": int(row[9]),
                                }
                                record["endpoint"] = {
                                    "object": row[10],
                                    "instance": row[11],
                                    "port": row[12],
                                    "bit": int(row[13]),
                                }
                            if budgets is not None:budgets.attach(ordinal,record)
                        if (
                            record is None
                            or int(row[14]) != len(record["path_nets"])
                        ):
                            raise ValidationError(
                                "STA path database SQLite net ordering is invalid"
                            )
                        record["path_nets"].append(row[15])
                    if record is not None:
                        found.add(record["id"])
                        yield record
                    if budgets is not None:budgets.finish()
            except sqlite3.DatabaseError as error:
                raise ValidationError(
                    "STA path database SQLite payload is invalid"
                ) from error
            finally:
                connection.close()
            missing = sorted(requested - found)
            if missing:
                raise ValidationError(
                    "STA path database lacks selected paths: "
                    + ", ".join(repr(item) for item in missing[:10])
                )
            return

    found = set()
    for record in iter_sta_path_database_paths(
        path, value, verify_payload=verify_payload
    ):
        if record.get("id") in requested:
            found.add(record["id"])
            yield record
    missing = sorted(requested - found)
    if missing:
        raise ValidationError(
            "STA path database lacks selected paths: "
            + ", ".join(repr(item) for item in missing[:10])
        )


def sta_path_database_qor(
    database_path: Path, value: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    manifest = read_json(database_path) if value is None else value
    slacks = [
        float(record["slack_ns"])
        for record in iter_sta_path_database_paths(database_path, manifest)
    ]
    if not slacks:
        raise ValidationError("STA path database contains no timing paths")
    return {
        "wns_ns": min(slacks),
        "tns_ns": math.fsum(item for item in slacks if item < 0.0),
        "failing_endpoints": sum(item < 0.0 for item in slacks),
        "timed_endpoints": len(slacks),
    }


def load_sta_path_database(
    database_path: Path, *, verify_payload: bool = True
) -> Dict[str, Any]:
    """Load a TimingPathDB for an algorithm that intrinsically needs all paths.

    Indexed storage remains authoritative on disk. This explicit adapter is
    for algorithms whose optimization state is inherently whole-population;
    it must never be written back as a graph-scale JSON checkpoint.
    """

    manifest = read_json(database_path)
    if manifest.get("schema") == STA_PATH_DATABASE_SCHEMA:
        return manifest
    if manifest.get("schema") not in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    return {
        **manifest,
        "schema": STA_PATH_DATABASE_SCHEMA,
        "paths": list(
            iter_sta_path_database_paths(
                database_path,
                manifest,
                verify_payload=verify_payload,
            )
        ),
    }


def _instance_pin_inventory(ir: EmuIR) -> Dict[str, set[tuple[str, int]]]:
    result: Dict[str, set[tuple[str, int]]] = {
        instance["id"]: set() for instance in ir.value["instances"]
    }
    for net in ir.value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                instance = endpoint["instance"]
                if instance is not None:
                    result[instance].add((endpoint["port"], endpoint["bit"]))
    for instance in ir.value["instances"]:
        for endpoint in instance.get("constant_connections", []):
            result[instance["id"]].add((endpoint["port"], endpoint["bit"]))
    return result


def _scalar_pin(port: str, bit: int, pins: set[tuple[str, int]]) -> str:
    width = max(
        (
            candidate_bit + 1
            for candidate_port, candidate_bit in pins
            if candidate_port == port
        ),
        default=0,
    )
    return port if width <= 1 else f"{port}__{bit}"


def sta_object_index(ir: EmuIR) -> Dict[str, Dict[str, Any]]:
    """Map provider object names to stable EmuIR endpoint identities."""
    if hasattr(ir, "object_index"):
        return ir.object_index
    pins = _instance_pin_inventory(ir)
    canonical: Dict[str, Dict[str, Any]] = {}
    for instance in ir.value["instances"]:
        instance_id = instance["id"]
        for port, bit in pins[instance_id]:
            name = f"{instance_id}/{_scalar_pin(port, bit, pins[instance_id])}"
            canonical[name] = {
                "object": name,
                "instance": instance_id,
                "port": port,
                "bit": bit,
            }
    for port in ir.value["ports"]:
        for bit in range(port["width"]):
            names = [port["id"]]
            if port["width"] > 1:
                names = [f"{port['id']}[{bit}]", f"{port['id']}__{bit}"]
            for name in names:
                canonical[name] = {
                    "object": name,
                    "instance": None,
                    "port": port["id"],
                    "bit": bit,
                }

    # OpenSTA reports Verilog escaped identifiers through Tcl.  A literal
    # backslash in the mapped-Verilog object name is therefore escaped one
    # additional time in ``get_full_name`` and in the exported path ID.  Add
    # that exact provider spelling as an O(1) alias while retaining the
    # provider-neutral EmuIR spelling in the endpoint certificate.
    #
    # Do not guess through an ambiguous name.  It is possible (although very
    # unusual) for one canonical object to contain one literal backslash and
    # another to contain two.  Such an IR cannot be mapped bijectively through
    # OpenSTA's textual path identity, so reject it instead of silently binding
    # the path to the wrong endpoint.
    result = dict(canonical)
    for name, endpoint in canonical.items():
        alias = name.replace("\\", "\\\\")
        if alias == name:
            continue
        existing = result.get(alias)
        if existing is not None and existing != endpoint:
            raise ValidationError(
                "STA provider object alias is ambiguous between "
                f"{existing['object']!r} and {name!r}"
            )
        result[alias] = endpoint
    return result


def sta_path_endpoints(
    path: Mapping[str, Any],
    object_index: Mapping[str, Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve structured or legacy provider path endpoints."""
    raw_start = path.get("startpoint")
    raw_end = path.get("endpoint")
    if isinstance(raw_start, dict) and isinstance(raw_end, dict):
        return dict(raw_start), dict(raw_end)
    path_id = path.get("id")
    match = (
        re.fullmatch(r"(.+)->(.+)#\d{8}", path_id)
        if isinstance(path_id, str)
        else None
    )
    if match is None:
        raise ValidationError(
            f"STA path {path_id!r} lacks provider-neutral endpoint identity"
        )
    missing = [
        name for name in match.groups() if name not in object_index
    ]
    if missing:
        raise ValidationError(
            f"STA path {path_id!r} has unmapped endpoint objects {missing}"
        )
    return (
        dict(object_index[match.group(1)]),
        dict(object_index[match.group(2)]),
    )


def _validate_endpoint_identity(
    endpoint: Any,
    known_instances: set[str],
    context: str,
) -> None:
    if not isinstance(endpoint, dict) or set(endpoint) != {
        "object",
        "instance",
        "port",
        "bit",
    }:
        raise ValidationError(f"{context} is invalid")
    if (
        not isinstance(endpoint["object"], str)
        or not endpoint["object"]
        or not isinstance(endpoint["port"], str)
        or not endpoint["port"]
        or isinstance(endpoint["bit"], bool)
        or not isinstance(endpoint["bit"], int)
        or endpoint["bit"] < 0
    ):
        raise ValidationError(f"{context} is invalid")
    instance = endpoint["instance"]
    if instance is not None and (
        not isinstance(instance, str)
        or instance not in known_instances
    ):
        raise ValidationError(f"{context}.instance is invalid")


def _hex_encode(value: str) -> str:
    return value.encode("utf-8").hex()


def _hex_decode(value: str, context: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"{context}: invalid UTF-8 hex") from error


def _database_normalization(paths: list[Dict[str, Any]]) -> Dict[str, float]:
    positive_scale = max(
        (path["slack_ns"] for path in paths if path["slack_ns"] >= 0.0),
        default=1.0,
    )
    if positive_scale == 0.0:
        positive_scale = 1.0
    negative_scale = abs(
        min(
            (path["slack_ns"] for path in paths if path["slack_ns"] < 0.0),
            default=-1.0,
        )
    )
    max_period = max(path["clock_period_ns"] for path in paths)
    return {
        "positive_slack_scale_ns": positive_scale,
        "negative_slack_scale_ns": negative_scale,
        "max_clock_period_ns": max_period,
    }


def _validate_database_normalization(
    value: Any,
) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise ValidationError("STA path database normalization is invalid")
    expected_keys = {
        "positive_slack_scale_ns",
        "negative_slack_scale_ns",
        "max_clock_period_ns",
    }
    if set(value) != expected_keys:
        raise ValidationError("STA path database normalization is invalid")
    result = {}
    for key in sorted(expected_keys):
        item = value[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or float(item) <= 0.0
        ):
            raise ValidationError(
                f"STA path database normalization.{key} is invalid"
            )
        result[key] = float(item)
    return result


def _normalized_slack(
    period: float,
    slack: float,
    normalization: Mapping[str, float],
) -> float:
    if slack >= 0.0:
        return (
            slack
            * period
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
        )
    return (
        slack
        / (
            normalization["negative_slack_scale_ns"]
            * period
        )
    )


def _write_net_map(
    ir_path: Path,
    output_path: Path,
    header: str,
    *,
    _ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    from .emuir_storage import is_emuir_sqlite
    from .emuir_compact import is_compact_emuir, write_compact_emuir
    from .store_publication import StoreSource
    from .native_protocol import CheckedRequestWriter
    from .native_tools import resolve_native_executable
    from .tool_runtime import NativeToolProcess

    if header != EMUIR_NET_MAP_HEADER:
        raise ValidationError('unsupported source net-map header')
    original = bind_store_source(ir_path, 'sta-source-document-v1')
    validate_store_destination(output_path, (original,))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-net-map-', dir=output_path.parent) as temporary:
        root = Path(temporary)
        if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
            # Preserve the caller's initial lease; do not renew it after the
            # format probe. The native producer reads indexed source rows.
            parents = (StoreSource(original.path, 'emuir-compact-v2', original.generation),)
        else:
            # Legacy import reuses the existing serializer. The actual map
            # and alias algorithm is the same native producer in both cases.
            ir = _ir if _ir is not None else EmuIR.load(ir_path)
            compact = root/'source.emuir.sqlite'
            write_compact_emuir(compact, ir.value, source_bindings=(original,))
            parents = (bind_store_source(compact, 'emuir-compact-v2'), original)
        request = root/'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31514d4e534d5545, 1, len(parents))
            for parent in parents:
                writer.put('QQQqqqq', *parent.generation)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [executable, 'phase2', 'export-net-map',
                   *(str(p.path) for p in parents), str(request), str(output_path)]
        with NativeToolProcess(executable, command, scratch_parent=root, maximum_bytes=65536) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native source net-map export failed: ' + completed.stdout)
        require_store_sources_unchanged(parents)
        try:
            result = json.loads(completed.stdout)
        except (ValueError, TypeError) as error:
            raise ValidationError('native source net-map lacks its terminal summary') from error
        if (not isinstance(result, dict) or result.get('schema') != 'emuflow.source-net-map/v1'
                or result.get('status') != 'pass'):
            raise ValidationError('native source net-map summary is invalid')
    return {**result, 'output': str(output_path)}


def write_emuir_net_map(
    ir_path: Path,
    output_path: Path,
    *,
    _ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    return _write_net_map(ir_path, output_path, EMUIR_NET_MAP_HEADER, _ir=_ir)


def _parse_sta_path_database_tsv_record(
    line: str,
    index: int,
    *,
    known_nets: set[str],
    object_index: Mapping[str, Mapping[str, Any]],
    clock_budgets: bool = False,
) -> Dict[str, Any]:
    fields = line.split("\t")
    if len(fields) != 6+(len(CLOCK_BUDGET_FIELDS)if clock_budgets else 0):
        raise ValidationError(
            f"STA path database TSV line {index}: invalid field count"
        )
    path_id = _hex_decode(
        fields[0], f"STA path database TSV line {index} path"
    )
    clock_domain = _hex_decode(
        fields[1], f"STA path database TSV line {index} clock"
    )
    if not path_id or not clock_domain:
        raise ValidationError(
            f"STA path database TSV line {index}: invalid path identity"
        )
    try:
        clock_period = float(fields[2])
        slack = float(fields[3])
        fixed_delay = float(fields[4])
    except ValueError as error:
        raise ValidationError(
            f"STA path database TSV line {index}: invalid numeric field"
        ) from error
    if (
        not all(
            math.isfinite(value)
            for value in (clock_period, slack, fixed_delay)
        )
        or clock_period <= 0.0
        or fixed_delay < 0.0
    ):
        raise ValidationError(
            f"STA path database TSV line {index}: invalid period/delay"
        )
    raw_nets = fields[5].split(",")
    if not raw_nets or any(not item for item in raw_nets):
        raise ValidationError(
            f"STA path database TSV line {index}: empty path-net list"
        )
    path_nets = [
        _hex_decode(
            item, f"STA path database TSV line {index} path_nets_hex"
        )
        for item in raw_nets
    ]
    if len(set(path_nets)) != len(path_nets):
        raise ValidationError(
            f"STA path database TSV line {index}: duplicate net"
        )
    unknown = sorted(set(path_nets) - known_nets)
    if unknown:
        raise ValidationError(
            f"STA path database TSV line {index}: unknown EmuIR nets {unknown}"
        )
    record = {
        "id": path_id,
        "clock_domain": clock_domain,
        "clock_period_ns": clock_period,
        "slack_ns": slack,
        "fixed_delay_ns": fixed_delay,
        "path_nets": path_nets,
    }
    if clock_budgets:parse_tsv_budget(fields[6:],record)
    try:
        startpoint, endpoint = sta_path_endpoints(record, object_index)
    except ValidationError:
        pass
    else:
        record["startpoint"] = startpoint
        record["endpoint"] = endpoint
    return record


def import_sta_path_database_tsv(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    provider: str,
    source: Optional[Mapping[str, Any]] = None,
    _ir: Optional[EmuIR] = None,
    _return_value: bool = False,
    expected_records: Optional[int] = None,
) -> Dict[str, Any]:
    if provider not in STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
    if expected_records is not None and (isinstance(expected_records,bool)or not isinstance(expected_records,int)or expected_records<=0):
        raise ValidationError('STA expected producer record count is invalid')
    with input_path.open()as stream:
        budget_format=stream.readline().rstrip('\r\n')==STA_PATH_DATABASE_CLOCK_TSV_HEADER
    if source is not None and 'clock_budget_policy'in source and source['clock_budget_policy']!=(
            'engine-edge-v1'if budget_format else'legacy-absolute-arrival-v1'):
        raise ValidationError('STA clock budget policy/header mismatch')
    if budget_format:
        report=import_sta_path_database_tsv_streaming(input_path,ir_path,output_path,
            provider=provider,source=source,_ir=_ir,expected_records=expected_records)
        if _return_value:report['_value']=load_sta_path_database(output_path,verify_payload=False)
        return report
    # Large physical timing graphs can exceed hundreds of MiB.  The OpenSTA
    # producer already owns the validated EmuIR object, so let that producer
    # pass the object through rather than reparsing the same JSON solely for
    # TSV import.  The private arguments deliberately stay out of the public
    # CLI contract; ordinary standalone callers retain the path-based load.
    ir = _ir if _ir is not None else EmuIR.load(ir_path)
    known_nets = ir.net_ids if hasattr(ir, "net_ids") else {net["id"] for net in ir.value["nets"]}
    object_index = sta_object_index(ir)
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != STA_PATH_DATABASE_TSV_HEADER:
        raise ValidationError("STA path database TSV: invalid header")
    paths = []
    path_ids = set()
    for index, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        record = _parse_sta_path_database_tsv_record(
            line,
            index,
            known_nets=known_nets,
            object_index=object_index,
        )
        path_id = record["id"]
        if path_id in path_ids:
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "invalid or duplicate path"
            )
        path_ids.add(path_id)
        paths.append(record)
    if not paths:
        raise ValidationError(
            "STA path database TSV contains no mapped timing paths"
        )
    if expected_records is not None and len(paths)!=expected_records:
        raise ValidationError('STA producer terminal record count differs')
    normalization = _database_normalization(paths)
    for path in paths:
        path["normalized_slack"] = _normalized_slack(
            path["clock_period_ns"],
            path["slack_ns"],
            normalization,
        )
    source_value = dict(source) if source is not None else {}
    source_value.update(
        {
            "provider": provider,
            "input": str(input_path),
        }
    )
    artifact = {
        "schema": STA_PATH_DATABASE_SCHEMA,
        "design": ir.value["design"]["name"],
        "source": source_value,
        "normalization": normalization,
        "paths": paths,
    }
    write_json(output_path, artifact)
    result = {
        "status": "pass",
        "design": artifact["design"],
        "paths": len(paths),
        "structured_endpoint_paths": sum(
            "startpoint" in path and "endpoint" in path for path in paths
        ),
        "unique_path_nets": len(
            {net for path in paths for net in path["path_nets"]}
        ),
        "output": str(output_path),
    }
    if _return_value:
        result["_value"] = artifact
    return result


def _adopt_sta_payload(temporary, selector, kind, schema, sources):
    """Commit an immutable SQL generation; the selector remains the public commit."""
    digest = _file_sha256(temporary)
    payload = selector.with_name(f"{selector.name}.{kind}.{digest}.sqlite")
    validate_store_destination(payload, sources)
    if payload.is_symlink():
        raise ValidationError("STA payload generation is a symbolic link")
    created = not payload.exists()
    if created:
        publish_sqlite_store(temporary, payload, schema, sources=sources)
    else:
        require_store_sources_unchanged(sources)
        if _file_sha256(payload) != digest:
            raise ValidationError("STA payload generation seal disagrees")
        temporary.unlink()
    return payload, digest, created


def _sta_ir_source_bindings(path, supplied_ir=None):
    # In-memory reference inputs do not claim a missing provenance filename.
    if supplied_ir is not None and not path.is_file():
        return ()
    from .emuir_storage import is_emuir_sqlite
    from .emuir_compact import is_compact_emuir

    sqlite_input = is_emuir_sqlite(path)
    schema = (
        "emuir-compact-v2"
        if sqlite_input and is_compact_emuir(path)
        else "emuir-indexed-v1" if sqlite_input else "sta-source-document-v1"
    )
    return (bind_store_source(path, schema),)


def import_sta_path_database_tsv_streaming(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    provider: str,
    source: Optional[Mapping[str, Any]] = None,
    _ir: Optional[EmuIR] = None,
    expected_records: Optional[int] = None,
) -> Dict[str, Any]:
    """Convert an STA TSV to a small manifest plus an indexed SQLite payload.

    One pass validates identities, derives global normalization, and writes
    normalized relational rows atomically. At no point is the path population
    materialized as a Python list, JSONL stream, or monolithic JSON document.
    """

    if provider not in STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
    if expected_records is not None and (isinstance(expected_records,bool)or not isinstance(expected_records,int)or expected_records<=0):
        raise ValidationError('STA expected producer record count is invalid')
    if output_path.is_symlink():
        raise ValidationError("STA selector is a symbolic link")
    source_bindings = validate_store_destination(
        output_path,
        (bind_store_source(input_path, "sta-source-document-v1"),)
        + _sta_ir_source_bindings(ir_path, _ir),
    )
    ir = _ir if _ir is not None else EmuIR.load(ir_path)
    known_nets = ir.net_ids if hasattr(ir, "net_ids") else {net["id"] for net in ir.value["nets"]}
    object_index = sta_object_index(ir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path = output_path.with_name(output_path.name + ".paths.sqlite")
    validate_store_destination(payload_path, source_bindings)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=payload_path.name + ".",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    path_ids: set[str] = set()
    path_nets_union: set[str] = set()
    positive_scale_value: Optional[float] = None
    most_negative_value: Optional[float] = None
    max_period = 0.0
    count = 0
    structured = 0
    negative_slacks: list[float] = []
    worst_slack: Optional[float] = None
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(temporary_path)
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            PRAGMA user_version = 1;
            CREATE TABLE paths (
                ordinal INTEGER PRIMARY KEY,
                path_id TEXT NOT NULL UNIQUE,
                clock_domain TEXT NOT NULL,
                clock_period_ns REAL NOT NULL,
                slack_ns REAL NOT NULL,
                fixed_delay_ns REAL NOT NULL,
                start_object TEXT,
                start_instance TEXT,
                start_port TEXT,
                start_bit INTEGER,
                end_object TEXT,
                end_instance TEXT,
                end_port TEXT,
                end_bit INTEGER
            );
            CREATE TABLE path_nets (
                path_ordinal INTEGER NOT NULL,
                net_ordinal INTEGER NOT NULL,
                net_id TEXT NOT NULL,
                PRIMARY KEY (path_ordinal, net_ordinal),
                FOREIGN KEY (path_ordinal) REFERENCES paths(ordinal)
            );
            CREATE INDEX path_nets_net_id ON path_nets(net_id);
            """
        )
        with input_path.open("r", encoding="utf-8") as source_stream:
            header = source_stream.readline().rstrip("\r\n")
            if header not in {STA_PATH_DATABASE_TSV_HEADER,STA_PATH_DATABASE_CLOCK_TSV_HEADER}:
                raise ValidationError("STA path database TSV: invalid header")
            clock_budgets=header==STA_PATH_DATABASE_CLOCK_TSV_HEADER
            if source is not None and 'clock_budget_policy'in source and source['clock_budget_policy']!=(
                    'engine-edge-v1'if clock_budgets else'legacy-absolute-arrival-v1'):
                raise ValidationError('STA clock budget policy/header mismatch')
            if clock_budgets:
                connection.execute('PRAGMA user_version=2')
                connection.executescript(CLOCK_BUDGET_DDL)
            for index, raw in enumerate(source_stream, start=2):
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                record = _parse_sta_path_database_tsv_record(
                    line,
                    index,
                    known_nets=known_nets,
                    object_index=object_index,
                    clock_budgets=clock_budgets,
                )
                path_id = record["id"]
                if path_id in path_ids:
                    raise ValidationError(
                        f"STA path database TSV line {index}: duplicate path"
                    )
                path_ids.add(path_id)
                path_nets_union.update(record["path_nets"])
                slack = float(record["slack_ns"])
                period = float(record["clock_period_ns"])
                if slack >= 0.0:
                    positive_scale_value = (
                        slack
                        if positive_scale_value is None
                        else max(positive_scale_value, slack)
                    )
                else:
                    most_negative_value = (
                        slack
                        if most_negative_value is None
                        else min(most_negative_value, slack)
                    )
                    negative_slacks.append(slack)
                max_period = max(max_period, period)
                worst_slack = (
                    slack if worst_slack is None else min(worst_slack, slack)
                )
                structured += int(
                    "startpoint" in record and "endpoint" in record
                )
                startpoint = record.get("startpoint")
                endpoint = record.get("endpoint")
                connection.execute(
                    """
                    INSERT INTO paths VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        count,
                        path_id,
                        record["clock_domain"],
                        period,
                        slack,
                        float(record["fixed_delay_ns"]),
                        startpoint.get("object") if startpoint else None,
                        startpoint.get("instance") if startpoint else None,
                        startpoint.get("port") if startpoint else None,
                        startpoint.get("bit") if startpoint else None,
                        endpoint.get("object") if endpoint else None,
                        endpoint.get("instance") if endpoint else None,
                        endpoint.get("port") if endpoint else None,
                        endpoint.get("bit") if endpoint else None,
                    ),
                )
                connection.executemany(
                    "INSERT INTO path_nets VALUES (?, ?, ?)",
                    (
                        (count, net_ordinal, net_id)
                        for net_ordinal, net_id in enumerate(
                            record["path_nets"]
                        )
                    ),
                )
                if clock_budgets:
                    connection.execute('INSERT INTO clock_budgets VALUES ('+','.join('?'for _ in range(1+len(CLOCK_BUDGET_FIELDS)))+')',
                        (count,*(record['clock_budget'][key]for key in CLOCK_BUDGET_FIELDS)))
                count += 1
        if count == 0:
            raise ValidationError(
                "STA path database TSV contains no mapped timing paths"
            )
        if expected_records is not None and count!=expected_records:
            raise ValidationError('STA producer terminal record count differs')
        connection.commit()
        connection.close()
        connection = None
        payload_path, payload_sha256, created_payload = _adopt_sta_payload(
            temporary_path,
            output_path,
            "paths",
            CLOCK_STORE_SCHEMA if clock_budgets else "timing-path-store-v1",
            source_bindings,
        )
    finally:
        if connection is not None:
            connection.close()
        temporary_path.unlink(missing_ok=True)
    positive_scale = (
        1.0
        if positive_scale_value in {None, 0.0}
        else float(positive_scale_value)
    )
    most_negative = (
        -1.0 if most_negative_value is None else float(most_negative_value)
    )
    normalization = {
        "positive_slack_scale_ns": positive_scale,
        "negative_slack_scale_ns": abs(most_negative),
        "max_clock_period_ns": max_period,
    }

    source_value = dict(source) if source is not None else {}
    source_value.update({"provider": provider, "input": str(input_path)})
    if clock_budgets:source_value['clock_budget_policy']='engine-edge-v1'
    qor = {
        "wns_ns": worst_slack,
        "tns_ns": math.fsum(negative_slacks),
        "failing_endpoints": len(negative_slacks),
        "timed_endpoints": count,
    }
    artifact = {
        "schema": STA_PATH_DATABASE_CLOCK_SCHEMA if clock_budgets else STA_PATH_DATABASE_INDEX_SCHEMA,
        "design": ir.value["design"]["name"],
        "source": source_value,
        "normalization": normalization,
        "summary": {
            "paths": count,
            "structured_endpoint_paths": structured,
            "unique_path_nets": len(path_nets_union),
            "qor": qor,
        },
        "payloads": {
            "paths": {
                "format": STA_PATH_DATABASE_CLOCK_FORMAT if clock_budgets else STA_PATH_DATABASE_SQLITE_FORMAT,
                "path": payload_path.name,
                "sha256": payload_sha256,
                "records": count,
            }
        },
    }
    if clock_budgets:artifact['summary']['clock_budget_paths']=count
    commit_store_selector(
        output_path, artifact, source_bindings, payload_path, created_payload
    )
    return {
        "status": "pass",
        "design": artifact["design"],
        "paths": count,
        "structured_endpoint_paths": structured,
        "unique_path_nets": len(path_nets_union),
        "path_qor": qor,
        "output": str(output_path),
    }


def _validate_sta_path_database_stream(
    database_path: Path,
    database: Mapping[str, Any],
    ir: EmuIR,
) -> Dict[str, Any]:
    if database.get("schema") not in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    if database.get("design") != ir.value["design"]["name"]:
        raise ValidationError("STA path database design does not match EmuIR")
    source = database.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provider") not in STA_PATH_DATABASE_PROVIDERS
    ):
        raise ValidationError("STA path database source is invalid")
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    known_nets = ir.net_ids if hasattr(ir, "net_ids") else {net["id"] for net in ir.value["nets"]}
    known_instances = ir.instance_ids if hasattr(ir, "instance_ids") else {instance["id"] for instance in ir.value["instances"]}
    path_ids: set[str] = set()
    clock_domains: set[str] = set()
    path_nets_union: set[str] = set()
    worst_slack: Optional[float] = None
    structured_endpoint_paths = 0
    negative_slacks: list[float] = []
    count = 0
    for index, path in enumerate(
        iter_sta_path_database_paths(database_path, database)
    ):
        context = f"STA path database paths[{index}]"
        if not isinstance(path, dict):
            raise ValidationError(f"{context} is invalid")
        required_keys = {
            "id", "clock_domain", "clock_period_ns", "slack_ns",
            "fixed_delay_ns", "path_nets", "normalized_slack",
        }
        endpoint_keys = {"startpoint", "endpoint"}
        if database.get('schema')==STA_PATH_DATABASE_CLOCK_SCHEMA or 'clock_budget'in path:
            required_keys.add('clock_budget')
            validate_clock_budget(path)
        if not (
            set(path) == required_keys
            or set(path) == required_keys | endpoint_keys
        ):
            raise ValidationError(f"{context} fields are invalid")
        if endpoint_keys <= set(path):
            structured_endpoint_paths += 1
            _validate_endpoint_identity(
                path["startpoint"], known_instances, f"{context}.startpoint"
            )
            _validate_endpoint_identity(
                path["endpoint"], known_instances, f"{context}.endpoint"
            )
        path_id = path["id"]
        clock_domain = path["clock_domain"]
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(f"{context} identity is invalid")
        path_ids.add(path_id)
        clock_domains.add(clock_domain)
        numeric: Dict[str, float] = {}
        for name in (
            "clock_period_ns", "slack_ns", "fixed_delay_ns",
            "normalized_slack",
        ):
            value = path[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(f"{context}.{name} is invalid")
            numeric[name] = float(value)
        if (
            numeric["clock_period_ns"] <= 0.0
            or numeric["fixed_delay_ns"] < 0.0
        ):
            raise ValidationError(f"{context} timing is invalid")
        path_nets = path["path_nets"]
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(f"{context}.path_nets is invalid")
        unknown = sorted(set(path_nets) - known_nets)
        if unknown:
            raise ValidationError(
                f"{context}.path_nets contains unknown EmuIR nets {unknown}"
            )
        expected_normalized = _normalized_slack(
            numeric["clock_period_ns"], numeric["slack_ns"], normalization
        )
        if abs(numeric["normalized_slack"] - expected_normalized) > 1.0e-12:
            raise ValidationError(
                f"{context}.normalized_slack is inconsistent"
            )
        path_nets_union.update(path_nets)
        slack = numeric["slack_ns"]
        worst_slack = slack if worst_slack is None else min(worst_slack, slack)
        if slack < 0.0:
            negative_slacks.append(slack)
        count += 1
    if count == 0:
        raise ValidationError("STA path database paths are invalid")
    summary = database.get("summary")
    recomputed_qor = {
        "wns_ns": worst_slack,
        "tns_ns": math.fsum(negative_slacks),
        "failing_endpoints": len(negative_slacks),
        "timed_endpoints": count,
    }
    if database.get('schema')==STA_PATH_DATABASE_CLOCK_SCHEMA and (
            not isinstance(summary,dict)or summary.get('clock_budget_paths')!=count):
        raise ValidationError('STA clock budget population summary disagrees')
    if not isinstance(summary, dict) or (
        summary.get("paths") != count
        or summary.get("structured_endpoint_paths")
        != structured_endpoint_paths
        or summary.get("unique_path_nets") != len(path_nets_union)
    ):
        raise ValidationError("STA path database summary disagrees")
    reported_qor = summary.get("qor")
    if not isinstance(reported_qor, dict):
        raise ValidationError("STA path database QoR summary is invalid")
    for field in ("wns_ns", "tns_ns"):
        if not math.isclose(
            float(reported_qor.get(field, math.nan)),
            float(recomputed_qor[field]),
            rel_tol=1.0e-12,
            abs_tol=1.0e-12,
        ):
            raise ValidationError("STA path database QoR summary disagrees")
    for field in ("failing_endpoints", "timed_endpoints"):
        if reported_qor.get(field) != recomputed_qor[field]:
            raise ValidationError("STA path database QoR summary disagrees")
    return {
        "status": "pass",
        "design": database["design"],
        "provider": source["provider"],
        "paths": count,
        "structured_endpoint_paths": structured_endpoint_paths,
        "unresolved_endpoint_paths": count - structured_endpoint_paths,
        "clock_domains": sorted(clock_domains),
        "unique_path_nets": len(path_nets_union),
        "worst_slack_ns": worst_slack,
        "path_qor": recomputed_qor,
    }


def validate_sta_path_database_value(
    database: Mapping[str, Any],
    ir: EmuIR,
) -> Dict[str, Any]:
    if database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("STA path database schema is invalid")
    if database.get("design") != ir.value["design"]["name"]:
        raise ValidationError("STA path database design does not match EmuIR")
    source = database.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provider") not in STA_PATH_DATABASE_PROVIDERS
    ):
        raise ValidationError("STA path database source is invalid")
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    raw_paths = database.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("STA path database paths are invalid")
    # IndexedTimingView exposes membership indexes backed by SQLite.  Do not
    # decode every net and retain million-entry Python ID sets merely to
    # validate the comparatively small set referenced by TimingPathDB.
    known_nets = getattr(ir, "net_ids", None)
    if known_nets is None:
        known_nets = {net["id"] for net in ir.value["nets"]}
    known_instances = getattr(ir, "instance_ids", None)
    if known_instances is None:
        known_instances = {
            instance["id"] for instance in ir.value["instances"]
        }
    path_ids = set()
    clock_domains = set()
    path_nets_union = set()
    worst_slack: Optional[float] = None
    structured_endpoint_paths = 0
    for index, path in enumerate(raw_paths):
        context = f"STA path database paths[{index}]"
        if not isinstance(path, dict):
            raise ValidationError(f"{context} is invalid")
        required_keys = {
            "id",
            "clock_domain",
            "clock_period_ns",
            "slack_ns",
            "fixed_delay_ns",
            "path_nets",
            "normalized_slack",
        }
        endpoint_keys = {"startpoint", "endpoint"}
        if database.get('schema')==STA_PATH_DATABASE_CLOCK_SCHEMA or 'clock_budget'in path:
            required_keys.add('clock_budget')
            validate_clock_budget(path)
        if not (
            set(path) == required_keys
            or set(path) == required_keys | endpoint_keys
        ):
            raise ValidationError(f"{context} fields are invalid")
        if endpoint_keys <= set(path):
            structured_endpoint_paths += 1
            _validate_endpoint_identity(
                path["startpoint"], known_instances, f"{context}.startpoint"
            )
            _validate_endpoint_identity(
                path["endpoint"], known_instances, f"{context}.endpoint"
            )
        path_id = path["id"]
        clock_domain = path["clock_domain"]
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(f"{context} identity is invalid")
        path_ids.add(path_id)
        clock_domains.add(clock_domain)
        numeric = {}
        for name in (
            "clock_period_ns",
            "slack_ns",
            "fixed_delay_ns",
            "normalized_slack",
        ):
            value = path[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(f"{context}.{name} is invalid")
            numeric[name] = float(value)
        if (
            numeric["clock_period_ns"] <= 0.0
            or numeric["fixed_delay_ns"] < 0.0
        ):
            raise ValidationError(f"{context} timing is invalid")
        path_nets = path["path_nets"]
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(f"{context}.path_nets is invalid")
        unknown = sorted(set(path_nets) - known_nets)
        if unknown:
            raise ValidationError(
                f"{context}.path_nets contains unknown EmuIR nets {unknown}"
            )
        expected_normalized = _normalized_slack(
            numeric["clock_period_ns"],
            numeric["slack_ns"],
            normalization,
        )
        if (
            abs(numeric["normalized_slack"] - expected_normalized)
            > 1.0e-12
        ):
            raise ValidationError(
                f"{context}.normalized_slack is inconsistent"
            )
        path_nets_union.update(path_nets)
        if worst_slack is None or numeric["slack_ns"] < worst_slack:
            worst_slack = numeric["slack_ns"]
    return {
        "status": "pass",
        "design": database["design"],
        "provider": source["provider"],
        "paths": len(raw_paths),
        "structured_endpoint_paths": structured_endpoint_paths,
        "unresolved_endpoint_paths": (
            len(raw_paths) - structured_endpoint_paths
        ),
        "clock_domains": sorted(clock_domains),
        "unique_path_nets": len(path_nets_union),
        "worst_slack_ns": worst_slack,
    }


def validate_sta_path_database(
    database_path: Path,
    ir_path: Path,
) -> Dict[str, Any]:
    database = read_json(database_path)
    from .emuir_compact import is_compact_emuir
    from .emuir_storage import is_emuir_sqlite
    from .indexed_timing import IndexedTimingView

    ir = (
        IndexedTimingView(ir_path)
        if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path)
        else EmuIR.load(ir_path)
    )
    try:
        if database.get("schema") in {
            STA_PATH_DATABASE_STREAM_SCHEMA,
            STA_PATH_DATABASE_INDEX_SCHEMA,
            STA_PATH_DATABASE_CLOCK_SCHEMA,
        }:
            return _validate_sta_path_database_stream(
                database_path, database, ir
            )
        return validate_sta_path_database_value(database, ir)
    finally:
        if isinstance(ir, IndexedTimingView):
            ir.close()


def validate_sta_path_database_path_value(
    database_path: Path,
    database: Mapping[str, Any],
    ir: EmuIR,
) -> Dict[str, Any]:
    """Validate an already-loaded manifest against an already-loaded IR."""

    if database.get("schema") in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        return _validate_sta_path_database_stream(
            database_path, database, ir
        )
    return validate_sta_path_database_value(database, ir)


def derive_partition_net_weights(
    database_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    criticality_scale: float = 9.0,
    criticality_exponent: float = 2.0,
    input_already_validated: bool = False,
) -> Dict[str, Any]:
    """Project path criticality onto hyperedges before partitioning.

    Net criticality is the maximum ``clamp(1 - slack / period, 0, 1)`` over
    all extracted paths containing the net. The power-law edge weight is the
    standard timing-driven partitioning form ``1 + scale * criticality^p``.
    """
    if output_path.is_symlink():
        raise ValidationError("STA selector is a symbolic link")
    source_bindings = validate_store_destination(
        output_path,
        (bind_store_source(database_path, "sta-path-selector-v1"),)
        + _sta_ir_source_bindings(ir_path),
    )
    database = read_json(database_path)
    if database.get("schema") in STA_PATH_DATABASE_INDEX_SCHEMAS:
        source_payload, _digest, _records, source_format = _sta_path_payload(
            database_path, database
        )
        source_bindings += (
            bind_store_source(
                source_payload,
                (
                    "timing-path-store-v1"
                    if source_format == STA_PATH_DATABASE_SQLITE_FORMAT
                    else CLOCK_STORE_SCHEMA if source_format==STA_PATH_DATABASE_CLOCK_FORMAT
                    else "sta-source-document-v1"
                ),
            ),
        )
    if input_already_validated:
        summary = database.get("summary")
        if not isinstance(summary, Mapping):
            raise ValidationError(
                "validated STA path database summary is missing"
            )
        checked = {"paths": int(summary["paths"])}
    else:
        checked = validate_sta_path_database(database_path, ir_path)
    for name, value in (
        ("criticality_scale", criticality_scale),
        ("criticality_exponent", criticality_exponent),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValidationError(f"{name} must be positive")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path = output_path.with_name(output_path.name + ".weights.sqlite")
    validate_store_destination(payload_path, source_bindings)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=payload_path.name + ".",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(temporary_path)
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            PRAGMA user_version = 1;
            CREATE TABLE net_weights (
                net_id TEXT PRIMARY KEY,
                criticality REAL NOT NULL,
                path_count INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            """
        )
        for path in iter_sta_path_database_paths(
            database_path, database, verify_payload=False
        ):
            criticality = max(
                0.0,
                min(
                    1.0,
                    1.0
                    - float(path["slack_ns"])
                    / float(path["clock_period_ns"]),
                ),
            )
            connection.executemany(
                """
                INSERT INTO net_weights(net_id, criticality, path_count, weight)
                VALUES (?, ?, 1, 1.0)
                ON CONFLICT(net_id) DO UPDATE SET
                    criticality = MAX(criticality, excluded.criticality),
                    path_count = path_count + 1
                """,
                ((net, criticality) for net in path["path_nets"]),
            )
        connection.create_function(
            "emuflow_weight",
            1,
            lambda criticality: 1.0
            + float(criticality_scale)
            * float(criticality) ** float(criticality_exponent),
            deterministic=True,
        )
        connection.execute(
            "UPDATE net_weights SET weight = emuflow_weight(criticality)"
        )
        connection.commit()
        summary_row = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN criticality > 0.0 THEN 1 ELSE 0 END),
                   MIN(CASE WHEN criticality > 0.0 THEN weight END),
                   MAX(CASE WHEN criticality > 0.0 THEN weight END)
            FROM net_weights
            """
        ).fetchone()
        connection.close()
        connection = None
        payload_path, payload_sha256, created_payload = _adopt_sta_payload(
            temporary_path,
            output_path,
            "weights",
            "net-weight-store-v1",
            source_bindings,
        )
    finally:
        if connection is not None:
            connection.close()
        temporary_path.unlink(missing_ok=True)

    timed_nets = int(summary_row[0] or 0)
    weighted_nets = int(summary_row[1] or 0)
    minimum_weight = float(summary_row[2] or 1.0)
    maximum_weight = float(summary_row[3] or 1.0)
    artifact = {
        "schema": PARTITION_NET_WEIGHTS_INDEX_SCHEMA,
        "design": database["design"],
        "source": {
            "provider": "sta-max-criticality-power-law-v2-indexed",
            # Bind the logical artifact identity, not a producer-local staging
            # directory. The payload and manifest digests provide the content
            # seal; embedding an absolute cache/run path would make identical
            # checkpoints differ across machines.
            "path_database": database_path.name,
            "path_database_provider": database["source"]["provider"],
        },
        "parameters": {
            "criticality_scale": float(criticality_scale),
            "criticality_exponent": float(criticality_exponent),
            "criticality_definition": "clamp(1-slack/period,0,1)",
            "path_reduction": "maximum",
        },
        "summary": {
            "paths": checked["paths"],
            "timed_nets": timed_nets,
            "weighted_nets": weighted_nets,
            "minimum_weight": minimum_weight,
            "maximum_weight": maximum_weight,
        },
        "payloads": {
            "weights": {
                "format": PARTITION_NET_WEIGHTS_SQLITE_FORMAT,
                "path": payload_path.name,
                "sha256": payload_sha256,
                "records": timed_nets,
            }
        },
    }
    commit_store_selector(
        output_path, artifact, source_bindings, payload_path, created_payload
    )
    return {
        "status": "pass",
        "design": database["design"],
        "paths": checked["paths"],
        "timed_nets": timed_nets,
        "weighted_nets": weighted_nets,
        "minimum_weight": minimum_weight,
        "maximum_weight": maximum_weight,
        "output": str(output_path),
    }


def derive_partition_net_weights_value(
    database: Mapping[str, Any],
    ir: EmuIR,
    output_path: Path,
    *,
    source_path_database: str,
    criticality_scale: float = 9.0,
    criticality_exponent: float = 2.0,
    input_already_validated: bool = False,
) -> Dict[str, Any]:
    """Derive partition weights from one already-loaded TimingPathDB.

    Managed timing checkpoints load the large database once, validate that
    object once, and reuse it here.  Standalone callers retain the same strong
    validation through the path-based wrapper above.
    """

    for name, value in (
        ("criticality_scale", criticality_scale),
        ("criticality_exponent", criticality_exponent),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValidationError(f"{name} must be positive")
    checked = (
        {
            "paths": len(database.get("paths", [])),
            "design": database.get("design"),
        }
        if input_already_validated
        else validate_sta_path_database_value(database, ir)
    )
    criticality_by_net: Dict[str, float] = {}
    path_count_by_net: Dict[str, int] = {}
    for path in database["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - float(path["slack_ns"])
                / float(path["clock_period_ns"]),
            ),
        )
        for net in path["path_nets"]:
            criticality_by_net[net] = max(
                criticality_by_net.get(net, 0.0),
                criticality,
            )
            path_count_by_net[net] = path_count_by_net.get(net, 0) + 1
    weights = {
        net: 1.0
        + float(criticality_scale)
        * criticality ** float(criticality_exponent)
        for net, criticality in sorted(criticality_by_net.items())
        if criticality > 0.0
    }
    artifact = {
        "schema": PARTITION_NET_WEIGHTS_SCHEMA,
        "design": database["design"],
        "source": {
            "provider": "sta-max-criticality-power-law-v1",
            "path_database": source_path_database,
            "path_database_provider": database["source"]["provider"],
        },
        "parameters": {
            "criticality_scale": float(criticality_scale),
            "criticality_exponent": float(criticality_exponent),
            "criticality_definition": "clamp(1-slack/period,0,1)",
            "path_reduction": "maximum",
        },
        "criticality": dict(sorted(criticality_by_net.items())),
        "path_count": dict(sorted(path_count_by_net.items())),
        "weights": weights,
    }
    write_json(output_path, artifact)
    return {
        "status": "pass",
        "design": database["design"],
        "paths": checked["paths"],
        "timed_nets": len(criticality_by_net),
        "weighted_nets": len(weights),
        "minimum_weight": min(weights.values(), default=1.0),
        "maximum_weight": max(weights.values(), default=1.0),
        "output": str(output_path),
    }


def iter_partition_net_weight_records(
    manifest_path: Path,
    manifest: Optional[Mapping[str, Any]] = None,
    *,
    verify_payload: bool = True,
) -> Iterator[tuple[str, float, int, float]]:
    """Yield indexed net-weight rows without expanding a graph-scale JSON."""

    value = read_json(manifest_path) if manifest is None else manifest
    if value.get("schema") != PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        raise ValidationError("partition net weights schema is invalid")
    payloads = value.get("payloads")
    descriptor = (
        payloads.get("weights") if isinstance(payloads, Mapping) else None
    )
    if not isinstance(descriptor, Mapping):
        raise ValidationError("partition net weights payload is invalid")
    relative = descriptor.get("path")
    expected_digest = descriptor.get("sha256")
    expected_records = descriptor.get("records")
    if (
        descriptor.get("format") != PARTITION_NET_WEIGHTS_SQLITE_FORMAT
        or not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or isinstance(expected_records, bool)
        or not isinstance(expected_records, int)
        or expected_records < 0
    ):
        raise ValidationError("partition net weights payload is invalid")
    payload_path = manifest_path.parent / relative
    if not payload_path.is_file():
        raise ValidationError("partition net weights payload is missing")
    if verify_payload:
        _verify_file_digest(
            payload_path,
            expected_digest,
            "partition net weights payload",
        )
    connection = _open_sqlite_read_only(payload_path)
    count = 0
    try:
        for row in connection.execute(
            """
            SELECT net_id, criticality, path_count, weight
            FROM net_weights ORDER BY net_id
            """
        ):
            count += 1
            yield str(row[0]), float(row[1]), int(row[2]), float(row[3])
    except sqlite3.DatabaseError as error:
        raise ValidationError(
            "partition net weights SQLite payload is invalid"
        ) from error
    finally:
        connection.close()
    if count != expected_records:
        raise ValidationError("partition net weights payload count disagrees")


def partition_net_weights_payload_path(
    manifest_path: Path,
    manifest: Optional[Mapping[str, Any]] = None,
) -> Optional[Path]:
    """Return the indexed weight payload owned by a weights manifest."""

    value = read_json(manifest_path) if manifest is None else manifest
    if value.get("schema") == PARTITION_NET_WEIGHTS_SCHEMA:
        return None
    if value.get("schema") != PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        raise ValidationError("partition net weights schema is invalid")
    payloads = value.get("payloads")
    descriptor = (
        payloads.get("weights") if isinstance(payloads, Mapping) else None
    )
    relative = descriptor.get("path") if isinstance(descriptor, Mapping) else None
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
    ):
        raise ValidationError("partition net weights payload is invalid")
    payload_path = manifest_path.parent / relative
    if not payload_path.is_file():
        raise ValidationError("partition net weights payload is missing")
    return payload_path


def load_partition_net_weights_indexed(path: Path) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for net_id, criticality, path_count, weight in (
        iter_partition_net_weight_records(path)
    ):
        if (
            not net_id
            or not math.isfinite(criticality)
            or criticality < 0.0
            or criticality > 1.0
            or path_count <= 0
            or not math.isfinite(weight)
            or weight <= 0.0
        ):
            raise ValidationError("partition net weights row is invalid")
        if criticality > 0.0:
            result[net_id] = weight
    return result


def validate_partition_net_weights(
    weights_path: Path,
    database_path: Path,
    ir_path: Path,
) -> Dict[str, Any]:
    """Independently reconstruct an indexed weight population."""

    weights = read_json(weights_path)
    if weights.get("schema") != PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        raise ValidationError("partition net weights schema is invalid")
    database = read_json(database_path)
    checked = validate_sta_path_database(database_path, ir_path)
    parameters = weights.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValidationError("partition net weights parameters are invalid")
    scale = float(parameters.get("criticality_scale"))
    exponent = float(parameters.get("criticality_exponent"))
    actual = {
        net: (criticality, path_count, weight)
        for net, criticality, path_count, weight in (
            iter_partition_net_weight_records(weights_path, weights)
        )
    }
    expected: Dict[str, tuple[float, int]] = {}
    for path in iter_sta_path_database_paths(
        database_path, database, verify_payload=False
    ):
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - float(path["slack_ns"])
                / float(path["clock_period_ns"]),
            ),
        )
        for net in path["path_nets"]:
            previous = expected.get(net, (0.0, 0))
            expected[net] = (
                max(previous[0], criticality),
                previous[1] + 1,
            )
    if set(actual) != set(expected):
        raise ValidationError("partition net weights coverage is invalid")
    for net, (criticality, path_count) in expected.items():
        actual_criticality, actual_count, actual_weight = actual[net]
        expected_weight = 1.0 + scale * criticality**exponent
        if (
            abs(actual_criticality - criticality) > 1.0e-12
            or actual_count != path_count
            or abs(actual_weight - expected_weight) > 1.0e-12
        ):
            raise ValidationError(
                f"partition net weight reconstruction failed for {net!r}"
            )
    summary = weights.get("summary")
    expected_summary = {
        "paths": checked["paths"],
        "timed_nets": len(expected),
        "weighted_nets": sum(value[0] > 0.0 for value in expected.values()),
        "minimum_weight": min(
            (
                1.0 + scale * value[0] ** exponent
                for value in expected.values()
                if value[0] > 0.0
            ),
            default=1.0,
        ),
        "maximum_weight": max(
            (
                1.0 + scale * value[0] ** exponent
                for value in expected.values()
                if value[0] > 0.0
            ),
            default=1.0,
        ),
    }
    if summary != expected_summary:
        raise ValidationError("partition net weights summary disagrees")
    return {"status": "pass", **expected_summary}


def project_sta_path_database(
    database_path: Path,
    assignment_path: Path,
    output_path: Path,
    *,
    input_already_validated: bool = False,
) -> Dict[str, Any]:
    database = read_json(database_path)
    assignment = read_phase3_cut_timing_view(
        assignment_path,
        include_instance_assignment=True,
    )
    artifact, summary = project_sta_path_database_value(
        database,
        assignment,
        source_input_sha256=_file_sha256(database_path),
        output_path=str(output_path),
        database_path=database_path,
        verify_payload=not input_already_validated,
    )
    write_json(output_path, artifact)
    return summary


def project_sta_path_database_value(
    database: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    source_input_sha256: str,
    output_path: str | None = None,
    database_path: Optional[Path] = None,
    verify_payload: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Project one loaded TimingPathDB onto one loaded assignment.

    ``source_input_sha256`` is supplied by the managed dependency certificate
    or computed once by the standalone wrapper.  This keeps provenance stable
    without re-reading an immutable multi-gigabyte input merely to hash it.
    """

    if re.fullmatch(r"[0-9a-f]{64}", source_input_sha256) is None:
        raise ValidationError("STA path projection input digest is invalid")
    if database.get("schema") not in {
        STA_PATH_DATABASE_SCHEMA,
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
        STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    if database.get("design") != assignment.get("design"):
        raise ValidationError(
            "STA path database design does not match assignment"
        )
    cut_nets = {
        cut["net"]
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict) and isinstance(cut.get("net"), str)
    }
    if not cut_nets:
        raise ValidationError(
            "STA path projection requires partition cut nets"
        )
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    cut_by_net = {
        cut["net"]: cut
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict)
        and isinstance(cut.get("net"), str)
        and cut["net"] in cut_nets
    }
    if set(cut_by_net) != cut_nets:
        raise ValidationError("partition cut-net records are invalid")
    cut_signature_by_net = {}
    for net in sorted(cut_by_net):
        cut = cut_by_net[net]
        sources = cut.get("source_fpgas", [])
        sinks = cut.get("sink_fpgas", [])
        if (
            not isinstance(sources, list)
            or not all(isinstance(item, str) and item for item in sources)
            or not isinstance(sinks, list)
            or not all(isinstance(item, str) and item for item in sinks)
        ):
            raise ValidationError(
                f"partition cut-net {net!r} endpoint lists are invalid"
            )
        cut_signature_by_net[net] = (
            f"{','.join(sources)}->{','.join(sinks)}"
        )
    instance_assignment = assignment.get("instance_assignment")
    if not isinstance(instance_assignment, Mapping):
        instance_assignment = {}

    def path_transitions(
        path: Mapping[str, Any], candidates: list[str]
    ) -> tuple[list[str], Optional[list[Dict[str, str]]]]:
        startpoint = path.get("startpoint")
        endpoint = path.get("endpoint")
        if not isinstance(startpoint, dict) or not isinstance(endpoint, dict):
            return candidates, None
        end_instance = endpoint.get("instance")
        end_partition = instance_assignment.get(end_instance)
        if not isinstance(end_partition, str) or not end_partition:
            return candidates, None
        target = end_partition
        reverse_transitions = []
        for net in reversed(candidates):
            raw_sources = cut_by_net[net].get("source_fpgas")
            raw_sinks = cut_by_net[net].get("sink_fpgas")
            if not isinstance(raw_sources, list) or len(raw_sources) != 1:
                return candidates, None
            source = raw_sources[0]
            if source == target:
                # This timing path follows a local fanout of a net whose
                # other sinks make the net globally cross-partition.
                continue
            if not isinstance(raw_sinks, list) or target not in raw_sinks:
                raise ValidationError(
                    f"STA path {path['id']!r} partition {target!r} cannot "
                    f"be reached through cut net {net!r}"
                )
            reverse_transitions.append(
                {"net": net, "from": source, "to": target}
            )
            target = source
        start_instance = startpoint.get("instance")
        start_partition = instance_assignment.get(start_instance)
        if (
            isinstance(start_partition, str)
            and start_partition != target
        ):
            raise ValidationError(
                f"STA path {path['id']!r} launch/capture partition chain "
                "is inconsistent"
            )
        transitions = list(reversed(reverse_transitions))
        return [item["net"] for item in transitions], transitions

    paths = []
    positive_scale_value: Optional[float] = None
    most_negative_value: Optional[float] = None
    max_period = 0.0
    covered_cut_nets = set()
    path_ids = set()
    if database.get("schema") == STA_PATH_DATABASE_SCHEMA:
        raw_paths = database.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValidationError("STA path database paths are invalid")
        path_records: Iterator[Mapping[str, Any]] = iter(raw_paths)
    else:
        if database_path is None:
            raise ValidationError(
                "indexed STA projection requires its manifest path"
            )
        path_records = iter_sta_path_database_paths(
            database_path, database, verify_payload=verify_payload
        )
    database_path_count = 0
    for index, path in enumerate(path_records):
        database_path_count += 1
        if not isinstance(path, dict):
            raise ValidationError(
                f"STA path database paths[{index}] is invalid"
            )
        path_id = path.get("id")
        clock_domain = path.get("clock_domain")
        clock_period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        fixed_delay = path.get("fixed_delay_ns")
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(
                f"STA path database paths[{index}] identity is invalid"
            )
        path_ids.add(path_id)
        for name, value in (
            ("clock_period_ns", clock_period),
            ("slack_ns", slack),
            ("fixed_delay_ns", fixed_delay),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"STA path database paths[{index}].{name} is invalid"
                )
        if float(clock_period) <= 0.0 or float(fixed_delay) < 0.0:
            raise ValidationError(
                f"STA path database paths[{index}] timing is invalid"
            )
        path_nets = path.get("path_nets")
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(
                f"STA path database paths[{index}].path_nets is invalid"
            )
        expected_normalized = _normalized_slack(
            float(clock_period),
            float(slack),
            normalization,
        )
        normalized = path.get("normalized_slack")
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, (int, float))
            or not math.isfinite(float(normalized))
            or abs(float(normalized) - expected_normalized) > 1.0e-12
        ):
            raise ValidationError(
                f"STA path database paths[{index}].normalized_slack "
                "is invalid"
            )
        numeric_period = float(clock_period)
        numeric_slack = float(slack)
        max_period = max(max_period, numeric_period)
        if numeric_slack >= 0.0:
            positive_scale_value = (
                numeric_slack
                if positive_scale_value is None
                else max(positive_scale_value, numeric_slack)
            )
        else:
            most_negative_value = (
                numeric_slack
                if most_negative_value is None
                else min(most_negative_value, numeric_slack)
            )
        candidates = [net for net in path_nets if net in cut_nets]
        projected, transitions = path_transitions(path, candidates)
        if not projected:
            continue
        covered_cut_nets.update(projected)
        record = {
            "id": path_id,
            "clock_domain": clock_domain,
            "clock_period_ns": float(clock_period),
            "slack_ns": float(slack),
            "fixed_delay_ns": float(fixed_delay),
            "cut_nets": projected,
            "cut_signature": [
                cut_signature_by_net[net]
                for net in projected
            ],
            "normalized_slack": expected_normalized,
            "compressed_path_ids": [path_id],
        }
        if transitions is not None:
            record["cut_transitions"] = transitions
        paths.append(record)
    if not paths:
        raise ValidationError(
            "STA path database has no path crossing this partition"
        )
    expected_normalization = {
        "positive_slack_scale_ns": (
            1.0
            if positive_scale_value in {None, 0.0}
            else float(positive_scale_value)
        ),
        "negative_slack_scale_ns": abs(
            -1.0
            if most_negative_value is None
            else float(most_negative_value)
        ),
        "max_clock_period_ns": max_period,
    }
    if any(
        abs(expected_normalization[key] - normalization[key]) > 1.0e-12
        for key in expected_normalization
    ):
        raise ValidationError(
            "STA path database normalization does not match its paths"
        )
    artifact = compress_sta_paths({
        "schema": STA_PATHS_SCHEMA,
        "design": assignment["design"],
        "source": {
            "provider": "partition-projected-sta-paths-v1",
            # Checkpoints are first built below a staging directory and then
            # atomically moved into the content-addressed object store.  A
            # producer-local absolute path would make an otherwise identical
            # projection fail independent reconstruction after that move.
            "input_sha256": source_input_sha256,
        },
        "normalization": normalization,
        "paths": paths,
    })
    return artifact, {
        "status": "pass",
        "design": assignment["design"],
        "database_paths": database_path_count,
        "projected_paths": len(paths),
        "compressed_paths": len(artifact["paths"]),
        "cut_nets": len(cut_nets),
        "covered_cut_nets": len(covered_cut_nets),
        "uncovered_cut_nets": len(cut_nets - covered_cut_nets),
        **({"output": output_path} if output_path is not None else {}),
    }
