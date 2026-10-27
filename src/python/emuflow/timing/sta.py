"""STA extraction adapters with stable EmuIR cut-net identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_store_source, publish_sqlite_store, commit_store_selector, require_store_sources_unchanged, validate_store_destination
from emuflow.common.io import read_json, write_json
from emuflow.ir.ir import EmuIR
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.partition.phase3_storage import read_phase3_assignment_view, read_phase3_cut_timing_view
from emuflow.scheduling.timing_routing import STA_PATHS_SCHEMA, compress_sta_paths


STA_PATH_DATABASE_SCHEMA = "emuflow.sta-path-database/v1"
STA_PATH_DATABASE_STREAM_SCHEMA = "emuflow.sta-path-database/v2"
STA_PATH_DATABASE_INDEX_SCHEMA = "emuflow.sta-path-database/v3"
STA_PATH_DATABASE_PAYLOAD_FORMAT_V1 = "jsonl-object/v1"
STA_PATH_DATABASE_PAYLOAD_FORMAT_V2 = "jsonl-sta-path-raw/v2"
STA_PATH_DATABASE_PAYLOAD_FORMAT = "jsonl-sta-path-row/v3"
STA_PATH_DATABASE_SQLITE_FORMAT = "sqlite-sta-path/v1"
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
    }:
        raise ValidationError("STA path database schema is invalid")
    return _sta_path_payload(manifest_path, value)[0]


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
    connection = sqlite3.connect(immutable_sqlite_uri(path), uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _verify_file_digest(path: Path, expected: str, context: str) -> None:
    if _file_sha256(path) != expected:
        raise ValidationError(f"{context} digest disagrees")


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
        }
        else None
    )
    if payload_format == STA_PATH_DATABASE_SQLITE_FORMAT:
        if schema != STA_PATH_DATABASE_INDEX_SCHEMA:
            raise ValidationError("STA path database payload/schema mismatch")
        if verify_payload:
            _verify_file_digest(
                payload_path,
                expected_digest,
                "STA path database payload",
            )
        connection = _open_sqlite_read_only(payload_path)
        try:
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
    # The caller may already own the validated graph. Exporting a two-column
    # view must not reconstruct a second multi-million-cell Python graph.
    ir = _ir if _ir is not None else EmuIR.load(ir_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        output.write(header + "\n")
        net_rows = ir.net_rows() if hasattr(ir, "net_rows") else (
            (index, net["id"]) for index, net in enumerate(ir.value["nets"])
        )
        for index, net_id in net_rows:
            output.write(
                f"{_hex_encode(f'__emuflow_net_{index}')}\t"
                f"{_hex_encode(net_id)}\n"
            )
    return {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "nets": len(ir.value["nets"]),
        "output": str(output_path),
    }


def write_emuir_net_map(
    ir_path: Path,
    output_path: Path,
    *,
    _ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    if _ir is None:
        from emuflow.ir.emuir_storage import is_emuir_sqlite
        from emuflow.ir.emuir_compact import is_compact_emuir
        if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
            from contextlib import closing
            from emuflow.timing.indexed_timing import IndexedTimingView
            with closing(IndexedTimingView(ir_path)) as view:
                return write_emuir_net_map(ir_path, output_path, _ir=view)
    if _ir is not None and hasattr(_ir, "source"):
        from emuflow.timing.opensta_export_native import export_opensta_inputs
        if _ir.source.path != Path(ir_path).resolve():
            raise ValidationError("net map belongs to another EmuIR source")
        report = export_opensta_inputs(_ir, net_map=output_path)
        return {"status": "pass", "design": _ir.value["design"]["name"],
                "nets": report["nets"], "output": str(output_path)}
    return _write_net_map(ir_path, output_path, EMUIR_NET_MAP_HEADER, _ir=_ir)


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
    from emuflow.ir.emuir_storage import is_emuir_sqlite
    from emuflow.ir.emuir_compact import is_compact_emuir

    sqlite_input = is_emuir_sqlite(path)
    schema = (
        "emuir-compact-v2"
        if sqlite_input and is_compact_emuir(path)
        else "emuir-indexed-v1" if sqlite_input else "sta-source-document-v1"
    )
    return (bind_store_source(path, schema),)


def import_sta_path_database_tsv_streaming(
    input_path: Path, ir_path: Path, output_path: Path, *, provider: str,
    source: Optional[Mapping[str, Any]] = None, _ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    """Import current compact timing inputs through the shared native core."""
    from emuflow.timing.sta_paths_native import import_paths
    return import_paths(input_path, ir_path, output_path, provider=provider,
                        source=source, view=_ir)


def _validate_sta_path_database_stream(
    database_path: Path,
    database: Mapping[str, Any],
    ir: EmuIR,
) -> Dict[str, Any]:
    if database.get("schema") not in {
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    from emuflow.timing.indexed_timing import IndexedTimingView
    descriptor = database.get("payloads", {}).get("paths", {})
    if (isinstance(ir, IndexedTimingView) and database.get("schema") == STA_PATH_DATABASE_INDEX_SCHEMA
            and descriptor.get("format") == STA_PATH_DATABASE_SQLITE_FORMAT):
        from emuflow.timing.sta_paths_native import validate_paths
        return validate_paths(database_path, database, ir)
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
    from emuflow.ir.emuir_compact import is_compact_emuir
    from emuflow.ir.emuir_storage import is_emuir_sqlite
    from emuflow.timing.indexed_timing import IndexedTimingView

    ir = (
        IndexedTimingView(ir_path)
        if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path)
        else EmuIR.load(ir_path)
    )
    try:
        if database.get("schema") in {
            STA_PATH_DATABASE_STREAM_SCHEMA,
            STA_PATH_DATABASE_INDEX_SCHEMA,
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
    }:
        return _validate_sta_path_database_stream(
            database_path, database, ir
        )
    return validate_sta_path_database_value(database, ir)


def derive_partition_net_weights(
    database_path: Path, ir_path: Path, output_path: Path, *,
    criticality_scale: float = 9.0, criticality_exponent: float = 2.0,
    _validation=None,
) -> Dict[str, Any]:
    """Project the current indexed path owner through the shared native core."""
    from emuflow.timing.sta_weights_native import build_weights
    return build_weights(database_path, ir_path, output_path,
        criticality_scale=criticality_scale, criticality_exponent=criticality_exponent,
        validation=_validation)


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
    weights_path: Path, database_path: Path, ir_path: Path,
) -> Dict[str, Any]:
    """Independently reduce source memberships without a Python net table."""
    from emuflow.timing.sta_weights_native import validate_weights
    return validate_weights(weights_path, database_path, ir_path)


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
