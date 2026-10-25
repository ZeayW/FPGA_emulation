"""Small packed-v3 assignment fixtures; no historical SQL payload producer."""

from contextlib import closing
import json
import sqlite3
from pathlib import Path

from emuflow.phase3_assignment_index import (
    ASSIGNMENT_INDEX_APPLICATION_ID,
    CORE_DDL,
    PACKED_EXACT_ASSIGNMENT_INDEX_SCHEMA,
)
from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT, write_phase3_exact_binary


def write_assignment_v3_fixture(
    path: Path, *, parts, semantic, metadata, cut_sinks=(), copies=()
):
    descriptor = write_phase3_exact_binary(
        path.with_name(EXACT_BINARY_ARTIFACT), semantic, list(parts),
        semantic_digest=metadata["semantic_contract_sha256"],
    )
    metadata = {
        **metadata,
        "schema": PACKED_EXACT_ASSIGNMENT_INDEX_SCHEMA,
        "semantic_contract_schema": semantic["schema"],
        "semantic_contract_binary": descriptor,
    }
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            f"PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};"
            "PRAGMA user_version=3;" + CORE_DDL
        )
        db.executemany("INSERT INTO metadata VALUES (?,?)", ((k, json.dumps(v)) for k, v in metadata.items()))
        db.executemany("INSERT INTO parts VALUES (?,?)", enumerate(parts))
        db.executemany("INSERT INTO cut_sinks VALUES (?,?,?)", cut_sinks)
        db.executemany("INSERT INTO copies VALUES (?,?,?,?)", copies)
