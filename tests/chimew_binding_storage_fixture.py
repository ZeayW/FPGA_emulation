"""Test-only fixture publisher; production uses the native typed owner."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow_tools.document_io import read_json
from emuflow.storage.store_publication import publish_sqlite_store, commit_store_selector, require_store_sources_unchanged, bind_store_source
from emuflow_tools.transport.chimew.chimew_binding_storage import ChimewBindingView

CHIMEW_BINDING_STORAGE_SCHEMA = "emuflow.chimew-binding-storage/v1"
CHIMEW_BINDING_APPLICATION_ID = 0x454D5542
CHIMEW_BINDING_USER_VERSION = 1
_LOGICAL_SCHEMAS = {
    "emuflow.chimew-phase6-electrical-binding/v1",
    "emuflow.chimew-phase6-electrical-binding/v2",
    "emuflow.chimew-phase6-electrical-binding/v3",
}

def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )

def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

def _logical_sha256(
    header: Mapping[str, Any], records: int, entries_sha256: str, members: int,
    members_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "header": header,
                "entries": {"records": records, "sha256": entries_sha256},
                "members": {"records": members, "sha256": members_sha256},
            }
        ).encode("utf-8")
    ).hexdigest()

def _start_canonical_document_hash(
    header: Mapping[str, Any],
) -> tuple[Any, list[str], int]:
    """Start a canonical object hash immediately before its entries array."""

    digest = hashlib.sha256()
    keys = sorted((*header.keys(), "entries"))
    entries_index = keys.index("entries")
    digest.update(b"{")
    for index, key in enumerate(keys[: entries_index + 1]):
        if index:
            digest.update(b",")
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        if key == "entries":
            digest.update(b"[")
        else:
            digest.update(_canonical(header[key]).encode("utf-8"))
    return digest, keys, entries_index

def _finish_canonical_document_hash(
    digest: Any,
    header: Mapping[str, Any],
    keys: list[str],
    entries_index: int,
) -> str:
    digest.update(b"]")
    for key in keys[entries_index + 1 :]:
        digest.update(b",")
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        digest.update(_canonical(header[key]).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()

def write_chimew_binding_storage(
    path: Path, binding: Mapping[str, Any], *, source_bindings=()
) -> ChimewBindingView:
    if binding.get("schema") not in _LOGICAL_SCHEMAS:
        raise ValidationError("Chimew binding has an unsupported schema")
    source_bindings = tuple(source_bindings)
    require_store_sources_unchanged(source_bindings)
    records = binding.get("entries")
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise ValidationError("Chimew binding entries must be iterable")
    header = {key: value for key, value in binding.items() if key != "entries"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_artifact: Path | None = None
    if path.is_file():
        try:
            previous = read_json(path)
        except (OSError, ValueError):
            previous = {}
        previous_name = previous.get("artifact")
        if (
            previous.get("schema") == CHIMEW_BINDING_STORAGE_SCHEMA
            and isinstance(previous_name, str)
            and Path(previous_name).name == previous_name
            and Path(previous_name).suffix == ".sqlite"
        ):
            previous_artifact = path.with_name(previous_name)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp")
    connection = sqlite3.connect(temporary)
    entries_digest = hashlib.sha256()
    members_digest = hashlib.sha256()
    group_count = 0
    member_count = 0
    canonical_digest, canonical_keys, entries_key_index = (
        _start_canonical_document_hash(header)
    )
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={CHIMEW_BINDING_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={CHIMEW_BINDING_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE groups(
              seq INTEGER NOT NULL UNIQUE,
              group_id TEXT PRIMARY KEY,
              link_id TEXT NOT NULL,
              fpga_a TEXT NOT NULL,
              fpga_b TEXT NOT NULL,
              physical_lane INTEGER NOT NULL,
              direction TEXT NOT NULL,
              package_pin_a TEXT NOT NULL,
              package_pin_b TEXT NOT NULL,
              group_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE members(
              member TEXT PRIMARY KEY,
              group_seq INTEGER NOT NULL,
              member_seq INTEGER NOT NULL,
              UNIQUE(group_seq,member_seq)
            ) WITHOUT ROWID;
            CREATE INDEX members_by_group
              ON members(group_seq,member_seq);
            """
        )
        group_rows: list[tuple[Any, ...]] = []
        member_rows: list[tuple[Any, ...]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise ValidationError("Chimew binding entry is malformed")
            full = dict(record)
            members = full.pop("schedule_entries", None)
            required_strings = (
                "group", "link", "fpga_a", "fpga_b", "direction",
                "package_pin_a", "package_pin_b",
            )
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(member, str) and member for member in members)
                or any(not isinstance(full.get(key), str) or not full[key]
                       for key in required_strings)
                or isinstance(full.get("physical_lane"), bool)
                or not isinstance(full.get("physical_lane"), int)
            ):
                raise ValidationError("Chimew binding entry is malformed")
            normalized = {**full, "schedule_entries": list(members)}
            encoded = _canonical(normalized)
            entries_digest.update(encoded.encode("utf-8"))
            entries_digest.update(b"\n")
            if group_count:
                canonical_digest.update(b",")
            canonical_digest.update(encoded.encode("utf-8"))
            group_rows.append(
                (
                    group_count,
                    full["group"],
                    full["link"],
                    full["fpga_a"],
                    full["fpga_b"],
                    full["physical_lane"],
                    full["direction"],
                    full["package_pin_a"],
                    full["package_pin_b"],
                    _canonical(full),
                )
            )
            for member_seq, member in enumerate(members):
                members_digest.update(
                    _canonical(
                        {
                            "group_seq": group_count,
                            "member_seq": member_seq,
                            "member": member,
                        }
                    ).encode("utf-8")
                )
                members_digest.update(b"\n")
                member_rows.append((member, group_count, member_seq))
                member_count += 1
            group_count += 1
            if len(member_rows) >= 4096:
                connection.executemany("INSERT INTO members VALUES (?,?,?)", member_rows)
                member_rows.clear()
            if len(group_rows) >= 1024:
                connection.executemany(
                    "INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?)", group_rows
                )
                group_rows.clear()
        if group_rows:
            connection.executemany(
                "INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?)", group_rows
            )
        if member_rows:
            connection.executemany("INSERT INTO members VALUES (?,?,?)", member_rows)
        entries_sha256 = entries_digest.hexdigest()
        members_sha256 = members_digest.hexdigest()
        logical_sha256 = _logical_sha256(
            header, group_count, entries_sha256, member_count, members_sha256
        )
        canonical_sha256 = _finish_canonical_document_hash(
            canonical_digest, header, canonical_keys, entries_key_index
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("entries_sha256", _canonical(entries_sha256)),
                ("members_sha256", _canonical(members_sha256)),
                ("logical_sha256", _canonical(logical_sha256)),
                ("canonical_sha256", _canonical(canonical_sha256)),
            ),
        )
        connection.commit()
        connection.close()
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "Chimew binding repeats a group or schedule entry"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    try:
        store_sha256 = file_sha256(temporary)
        artifact = path.with_name(f"{path.stem}.{store_sha256}.sqlite")
        created_artifact = not artifact.exists()
        if created_artifact:
            publish_sqlite_store(
                temporary, artifact, "chimew-binding-store-v1", sources=source_bindings
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("Chimew binding artifact digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": CHIMEW_BINDING_STORAGE_SCHEMA,
        "logical_schema": header["schema"],
        "artifact": artifact.name,
        "application_id": CHIMEW_BINDING_APPLICATION_ID,
        "user_version": CHIMEW_BINDING_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "entries": {"records": group_count, "sha256": entries_sha256},
        "members": {"records": member_count, "sha256": members_sha256},
        "logical_sha256": logical_sha256,
        "canonical_sha256": canonical_sha256,
    }
    view = commit_store_selector(
        path,
        manifest,
        source_bindings,
        artifact,
        created_artifact,
        validate=lambda: ChimewBindingView(path),
    )
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view
