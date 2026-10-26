"""Independent former STA path producer for small differential fixtures only."""
import math,os,sqlite3,tempfile
from pathlib import Path
from typing import Any,Dict,Mapping,Optional
from emuflow.ir import EmuIR
from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.sta import (STA_PATH_DATABASE_SCHEMA,_database_normalization,_normalized_slack,STA_PATH_DATABASE_PROVIDERS,STA_PATH_DATABASE_TSV_HEADER,STA_PATH_DATABASE_INDEX_SCHEMA,STA_PATH_DATABASE_SQLITE_FORMAT,_sta_ir_source_bindings,_parse_sta_path_database_tsv_record,sta_object_index,_adopt_sta_payload)
from emuflow.store_publication import bind_store_source,validate_store_destination,commit_store_selector

def import_sta_path_database_tsv_streaming(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    provider: str,
    source: Optional[Mapping[str, Any]] = None,
    _ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    """Convert an STA TSV to a small manifest plus an indexed SQLite payload.

    One pass validates identities, derives global normalization, and writes
    normalized relational rows atomically. At no point is the path population
    materialized as a Python list, JSONL stream, or monolithic JSON document.
    """

    if provider not in STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
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
            if header != STA_PATH_DATABASE_TSV_HEADER:
                raise ValidationError("STA path database TSV: invalid header")
            for index, raw in enumerate(source_stream, start=2):
                line = raw.rstrip("\r\n")
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
                count += 1
        if count == 0:
            raise ValidationError(
                "STA path database TSV contains no mapped timing paths"
            )
        connection.commit()
        connection.close()
        connection = None
        payload_path, payload_sha256, created_payload = _adopt_sta_payload(
            temporary_path,
            output_path,
            "paths",
            "timing-path-store-v1",
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
    qor = {
        "wns_ns": worst_slack,
        "tns_ns": math.fsum(negative_slacks),
        "failing_endpoints": len(negative_slacks),
        "timed_endpoints": count,
    }
    artifact = {
        "schema": STA_PATH_DATABASE_INDEX_SCHEMA,
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
                "format": STA_PATH_DATABASE_SQLITE_FORMAT,
                "path": payload_path.name,
                "sha256": payload_sha256,
                "records": count,
            }
        },
    }
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

def import_sta_path_database_tsv(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    provider: str,
    source: Optional[Mapping[str, Any]] = None,
    _ir: Optional[EmuIR] = None,
    _return_value: bool = False,
) -> Dict[str, Any]:
    if provider not in STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
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
