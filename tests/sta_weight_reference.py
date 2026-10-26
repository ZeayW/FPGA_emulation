"""Former weight producer/auditor, independent small-fixture oracle only."""
import math,os,sqlite3,tempfile
from pathlib import Path
from typing import Any,Dict,Mapping
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.sta import (STA_PATH_DATABASE_INDEX_SCHEMA,STA_PATH_DATABASE_SQLITE_FORMAT,PARTITION_NET_WEIGHTS_INDEX_SCHEMA,PARTITION_NET_WEIGHTS_SQLITE_FORMAT,_sta_ir_source_bindings,_sta_path_payload,_adopt_sta_payload,validate_sta_path_database,iter_sta_path_database_paths,iter_partition_net_weight_records)
from emuflow.store_publication import bind_store_source,validate_store_destination,commit_store_selector

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
    if database.get("schema") == STA_PATH_DATABASE_INDEX_SCHEMA:
        source_payload, _digest, _records, source_format = _sta_path_payload(
            database_path, database
        )
        source_bindings += (
            bind_store_source(
                source_payload,
                (
                    "timing-path-store-v1"
                    if source_format == STA_PATH_DATABASE_SQLITE_FORMAT
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
