"""Test-only report writer for malformed, historical and differential fixtures."""
from __future__ import annotations
import hashlib
import json
import math
import stat
import os
from pathlib import Path
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from typing import Any
import fcntl
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged, publish_sqlite_store, commit_store_selector
from emuflow.transport.chimew.chimew_bank_channel_report_storage import (
    REPORT_STORAGE_SCHEMA, REPORT_APPLICATION_ID, REPORT_USER_VERSION,
    BankChannelReportView as _CoreReportView, _AssignmentSequence, _canonical,
    _normalize_assignment, _logical_sha256, _report_lease, _connect, _string,
)

_SELECTOR_KEYS = {
    "schema",
    "logical_schema",
    "artifact",
    "application_id",
    "user_version",
    "store_bytes",
    "store_sha256",
    "header",
    "assignments",
    "logical_sha256",
}
_RELATION_SEAL_KEYS = {"records", "sha256"}
_HEADER_KEYS = {
    "schema",
    "status",
    "integration_status",
    "design",
    "platform",
    "provider",
    "paper_scope",
    "algorithm2_interpretation",
    "numeric_policy",
    "provenance",
    "input_sha256",
    "metrics",
    "direction_priority",
}


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )



def _normalize_header(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the bounded selector header without coercing identities."""

    from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

    if not isinstance(value, Mapping) or set(value) != _HEADER_KEYS:
        raise ValidationError("Chimew bank/channel report header has an invalid shape")

    def validate_json_tree(item: Any) -> bool:
        if item is None or isinstance(item, (bool, str, int)):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, list):
            return all(validate_json_tree(child) for child in item)
        if isinstance(item, Mapping):
            return all(
                isinstance(key, str) and validate_json_tree(child)
                for key, child in item.items()
            )
        return False

    if not validate_json_tree(value):
        raise ValidationError("Chimew bank/channel report header is not finite JSON")
    for key in (
        "status",
        "integration_status",
        "design",
        "platform",
        "provider",
        "paper_scope",
        "algorithm2_interpretation",
        "numeric_policy",
    ):
        _string(value.get(key), f"report.{key}")
    if value.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA:
        raise ValidationError("Chimew bank/channel report schema is invalid")
    if not _valid_digest(value.get("input_sha256")):
        raise ValidationError("Chimew bank/channel report input digest is invalid")
    if not isinstance(value.get("provenance"), Mapping):
        raise ValidationError("Chimew bank/channel report provenance is invalid")
    if not isinstance(value.get("metrics"), Mapping):
        raise ValidationError("Chimew bank/channel report metrics are invalid")
    priorities = value.get("direction_priority")
    if not isinstance(priorities, Mapping) or any(
        not isinstance(bank, str)
        or not bank
        or priority not in {"a_to_b", "b_to_a"}
        for bank, priority in priorities.items()
    ):
        raise ValidationError("Chimew bank/channel direction priority is invalid")
    return dict(value)



class BankChannelReportView(_CoreReportView):
    def _initialize(
        self, selector_path: Path, *, _verified_store_sha256: str | None
    ) -> None:
        from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

        self.manifest_path = selector_path.resolve(strict=True)
        selector_source = bind_store_source(
            self.manifest_path, "chimew-bank-report-selector-v1"
        )
        manifest = read_json(self.manifest_path)
        relation = manifest.get("assignments")
        artifact = manifest.get("artifact")
        if (
            set(manifest) != _SELECTOR_KEYS
            or manifest.get("schema") != REPORT_STORAGE_SCHEMA
            or manifest.get("logical_schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
            or manifest.get("application_id") != REPORT_APPLICATION_ID
            or manifest.get("user_version") != REPORT_USER_VERSION
            or not isinstance(manifest.get("header"), dict)
            or not isinstance(relation, dict)
            or set(relation) != _RELATION_SEAL_KEYS
            or isinstance(relation.get("records"), bool)
            or not isinstance(relation.get("records"), int)
            or relation["records"] <= 0
            or not _valid_digest(relation.get("sha256"))
            or not _valid_digest(manifest.get("logical_sha256"))
            or not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
            or not _valid_digest(manifest.get("store_sha256"))
        ):
            raise ValidationError("Chimew bank/channel report selector is malformed")
        self.store_path = self.manifest_path.with_name(artifact)
        try:
            store_source = bind_store_source(
                self.store_path, "chimew-bank-report-store-v1"
            )
            store_lstat = self.store_path.lstat()
            resolved_store = self.store_path.resolve(strict=True)
        except OSError as error:
            raise ValidationError(
                "Chimew bank/channel report store is missing"
            ) from error
        expected_store_sha256 = (
            _verified_store_sha256
            if _verified_store_sha256 is not None
            else file_sha256(self.store_path)
        )
        selector_stem = self.manifest_path.stem
        stem_parts = selector_stem.rsplit(".", 1)
        artifact_stem = (
            stem_parts[0]
            if len(stem_parts) == 2 and _valid_digest(stem_parts[1])
            else selector_stem
        )
        if (
            stat.S_ISLNK(store_lstat.st_mode)
            or resolved_store.parent != self.manifest_path.parent
            or resolved_store != self.store_path
            or isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != store_lstat.st_size
            or expected_store_sha256 != manifest["store_sha256"]
            or artifact
            != f"{artifact_stem}.{manifest['store_sha256']}.sqlite"
        ):
            raise ValidationError("Chimew bank/channel report store seal disagrees")
        connection = _connect(self.store_path)
        try:
            objects = tuple(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            columns = tuple(
                (row[1], row[2], int(row[3]), row[4], int(row[5]))
                for row in connection.execute("PRAGMA table_info(assignments)")
            )
            metadata_columns = tuple(
                (row[1], row[2], int(row[3]), row[4], int(row[5]))
                for row in connection.execute("PRAGMA table_info(metadata)")
            )
            metadata = {
                str(row[0]): json.loads(str(row[1]))
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            endpoints = connection.execute(
                "SELECT min(seq),max(seq),count(*),count(DISTINCT group_id),"
                "count(DISTINCT channel) FROM assignments"
            ).fetchone()
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != REPORT_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != REPORT_USER_VERSION
                or objects
                != (("table", "assignments"), ("table", "metadata"))
                or metadata_columns
                != (
                    ("key", "TEXT", 1, None, 1),
                    ("value", "TEXT", 1, None, 0),
                )
                or columns
                != (
                    ("seq", "INTEGER", 1, None, 1),
                    ("group_id", "TEXT", 1, None, 0),
                    ("bank_pair", "TEXT", 1, None, 0),
                    ("channel", "TEXT", 1, None, 0),
                    ("bank_cost_rank", "INTEGER", 1, None, 0),
                    ("channel_cost_rank", "INTEGER", 1, None, 0),
                    ("bank_cost", "REAL", 1, None, 0),
                    ("channel_cost", "REAL", 1, None, 0),
                )
                or set(metadata)
                != {"header", "assignments_sha256", "logical_sha256"}
                or metadata["header"] != manifest["header"]
                or metadata["assignments_sha256"] != relation["sha256"]
                or metadata["logical_sha256"] != manifest["logical_sha256"]
                or endpoints[0] != 0
                or endpoints[1] != relation["records"] - 1
                or endpoints[2] != relation["records"]
                or endpoints[3] != relation["records"]
                or endpoints[4] != relation["records"]
            ):
                raise ValidationError(
                    "Chimew bank/channel report SQLite metadata disagrees"
                )
        except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
            raise ValidationError(
                "Chimew bank/channel report SQLite store is corrupt"
            ) from error
        finally:
            connection.close()
        normalized_header = _normalize_header(manifest["header"])
        if normalized_header != manifest["header"]:
            raise ValidationError("Chimew bank/channel report header disagrees")
        self.manifest = manifest
        self.logical_sha256 = str(manifest["logical_sha256"])
        self._header = normalized_header
        count = int(relation["records"])
        self._assignments = _AssignmentSequence(
            self.store_path, count, lease_state=self._lease_state
        )
        self._assignments_by_group = _AssignmentSequence(
            self.store_path,
            count,
            order_by_group=True,
            lease_state=self._lease_state,
        )
        relation_digest = hashlib.sha256()
        for assignment in self._assignments:
            encoded = _canonical(assignment).encode("utf-8")
            relation_digest.update(encoded)
            relation_digest.update(b"\n")
        if (
            relation_digest.hexdigest() != relation["sha256"]
            or _logical_sha256(self._header, self._assignments)
            != self.logical_sha256
        ):
            raise ValidationError(
                "Chimew bank/channel report logical record seal disagrees"
            )

        self._source_bindings = (selector_source, store_source)
        require_store_sources_unchanged(self._source_bindings)

def _write_locked(
    path: Path,
    document: Mapping[str, Any],
    *,
    derive_physical_costs: bool,
    source_bindings,
) -> BankChannelReportView:
    from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

    if document.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA:
        raise ValidationError("Chimew bank/channel report schema is invalid")
    assignments = document.get("assignments")
    if not isinstance(assignments, Iterable):
        raise ValidationError("Chimew bank/channel assignments are missing")
    header = _normalize_header(
        {key: value for key, value in document.items() if key != "assignments"}
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    had_previous = path.is_file()
    previous_artifact: Path | None = None
    if had_previous:
        try:
            previous_view = BankChannelReportView(path, _acquire_lease=False)
        except (OSError, ValueError, ValidationError):
            # An invalid old selector is replaceable, but it is never trusted as
            # a GC authority.  Its named path may be attacker-controlled.
            previous_view = None
        if previous_view is not None:
            previous_artifact = previous_view.store_path
            previous_view.close()
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp")
    connection = sqlite3.connect(temporary)
    relation_digest = hashlib.sha256()
    rows: list[tuple[Any, ...]] = []
    count = 0
    stage1_physical_cost = 0.0
    stage2_physical_cost = 0.0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={REPORT_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={REPORT_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE assignments(
              seq INTEGER PRIMARY KEY,
              group_id TEXT NOT NULL UNIQUE,
              bank_pair TEXT NOT NULL,
              channel TEXT NOT NULL UNIQUE,
              bank_cost_rank INTEGER NOT NULL CHECK(bank_cost_rank>=0),
              channel_cost_rank INTEGER NOT NULL CHECK(channel_cost_rank>=0),
              bank_cost REAL NOT NULL CHECK(bank_cost>=0.0),
              channel_cost REAL NOT NULL CHECK(channel_cost>=0.0)
            ) WITHOUT ROWID;
            """
        )
        for seq, raw in enumerate(assignments):
            value = _normalize_assignment(raw, seq)
            encoded = _canonical(value).encode("utf-8")
            relation_digest.update(encoded)
            relation_digest.update(b"\n")
            stage1_physical_cost += value["bank_cost"]
            stage2_physical_cost += value["channel_cost"]
            rows.append(
                (
                    seq,
                    value["group"],
                    value["bank_pair"],
                    value["channel"],
                    value["bank_cost_rank"],
                    value["channel_cost_rank"],
                    value["bank_cost"],
                    value["channel_cost"],
                )
            )
            count = seq + 1
            if len(rows) >= 4096:
                connection.executemany(
                    "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)", rows
                )
                rows.clear()
        if rows:
            connection.executemany(
                "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)", rows
            )
        if count <= 0:
            raise ValidationError("Chimew bank/channel assignments are empty")
        if derive_physical_costs:
            metrics = dict(header["metrics"])
            metrics["stage1_physical_site_cost"] = stage1_physical_cost
            metrics["stage2_physical_site_cost"] = stage2_physical_cost
            header["metrics"] = metrics
        connection.commit()
        sequence = _AssignmentSequence(temporary, count)
        logical_sha256 = _logical_sha256(header, sequence)
        sequence.close()
        assignments_sha256 = relation_digest.hexdigest()
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("assignments_sha256", _canonical(assignments_sha256)),
                ("logical_sha256", _canonical(logical_sha256)),
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
            "Chimew bank/channel report repeats a group or channel"
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
        created = not artifact.exists()
        if created:
            publish_sqlite_store(
                temporary,
                artifact,
                "chimew-bank-report-store-v1",
                sources=source_bindings,
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("Chimew bank/channel report digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": REPORT_STORAGE_SCHEMA,
        "logical_schema": CHIMEW_BANK_CHANNEL_REPORT_SCHEMA,
        "artifact": artifact.name,
        "application_id": REPORT_APPLICATION_ID,
        "user_version": REPORT_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "assignments": {
            "records": count,
            "sha256": assignments_sha256,
        },
        "logical_sha256": logical_sha256,
    }
    checked = commit_store_selector(
        path,
        manifest,
        source_bindings,
        artifact,
        created,
        durable=True,
        validate=lambda: BankChannelReportView(path, _acquire_lease=False),
    )
    if previous_artifact is not None and previous_artifact != artifact:
        try:
            previous_artifact.unlink(missing_ok=True)
        except OSError:
            # The new selector is already committed and validated.  Failure to
            # retire an old immutable sidecar is a safe deferred-GC condition,
            # not a publication failure.
            pass
    return checked


def write_chimew_bank_channel_report_storage(
    path: Path,
    document: Mapping[str, Any],
    *,
    _derive_physical_costs: bool = False,
    source_bindings=(),
) -> BankChannelReportView:
    source_bindings = tuple(source_bindings)
    require_store_sources_unchanged(source_bindings)
    path = Path(path)
    if (
        getattr(document, "manifest_path", None) == path.resolve(strict=False)
    ):
        raise ValidationError(
            "cannot replace a Chimew bank/channel report from its active view"
        )
    lease = _report_lease(path, exclusive=True)
    descriptor = lease.__enter__()
    try:
        checked = _write_locked(
            path,
            document,
            derive_physical_costs=_derive_physical_costs,
            source_bindings=source_bindings,
        )
        checked.close()
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        view = BankChannelReportView(path, _acquire_lease=False)
        view._lease = lease
        return view
    except BaseException:
        lease.__exit__(None, None, None)
        raise
