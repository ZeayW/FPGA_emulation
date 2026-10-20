"""Single-file typed handoff for Xilinx packing and placement.

The native OpenPARF path produces one row-oriented SQLite artifact.  Legacy
PackedSiteNetlist and XilinxPlacement mappings are exposed as lazy views so
existing independently written validators and RapidWright exporters keep
checking the same contracts without serializing two graph-sized JSON files.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from .errors import ValidationError
from .io import file_sha256
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_PLACEMENT_SCHEMA


XILINX_PHYSICAL_HANDOFF_SCHEMA = "emuflow.xilinx-physical-handoff-sqlite/v1"
_APPLICATION_ID = 0x454D5550  # EMUP
_USER_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ValidationError("Xilinx physical handoff cannot be opened") from error
    connection.row_factory = sqlite3.Row
    return connection


class _ClusterSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, path: Path, *, placement: bool, count: int) -> None:
        self._path = path
        self._placement = placement
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self._path)
        try:
            cluster_rows = connection.execute(
                "SELECT * FROM cluster ORDER BY ordinal"
            )
            assignment_rows = iter(connection.execute(
                "SELECT * FROM assignment ORDER BY cluster_ordinal, "
                + ("placement_ordinal" if self._placement else "packed_ordinal")
            ))
            assignment = next(assignment_rows, None)
            for row in cluster_rows:
                values = []
                while (
                    assignment is not None
                    and assignment["cluster_ordinal"] == row["ordinal"]
                ):
                    if self._placement:
                        values.append({
                            "instance": assignment["instance"],
                            "cell_type": assignment["cell_type"],
                            "bel": assignment["bel"],
                            "placement_mode": assignment["placement_mode"],
                            "site": assignment["physical_site"],
                        })
                    else:
                        values.append({
                            "instance": assignment["instance"],
                            "cell_type": assignment["cell_type"],
                            "bel": assignment["bel"],
                        })
                    assignment = next(assignment_rows, None)
                if self._placement:
                    yield {
                        "cluster": row["cluster_id"], "site": row["site"],
                        "site_type": row["site_type"], "x": row["x"],
                        "y": row["y"], "fixed": bool(row["fixed"]),
                        "physical_region": _decode(row["physical_region_json"]),
                        "assignments": values,
                    }
                else:
                    cluster: Dict[str, Any] = {
                        "id": row["cluster_id"], "kind": row["kind"],
                        "site_templates": _decode(row["site_templates_json"]),
                        "control_set": _decode(row["control_set_json"]),
                        "assignments": values,
                    }
                    control_sets = _decode(row["control_sets_json"])
                    site_mode = row["site_mode"]
                    if control_sets is not None:
                        cluster["control_sets"] = control_sets
                    if site_mode is not None:
                        cluster["site_mode"] = site_mode
                    yield cluster
            if assignment is not None:
                raise ValidationError(
                    "Xilinx physical handoff contains orphan assignments"
                )
        except sqlite3.Error as error:
            raise ValidationError("Xilinx physical handoff rows are invalid") from error
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        values = list(self)
        return values[index]


def write_xilinx_physical_handoff(
    path: Path,
    packed: Mapping[str, Any],
    placement: Mapping[str, Any],
) -> None:
    """Atomically publish one normalized packing/placement database."""

    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("Xilinx physical handoff packing header is invalid")
    if placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx physical handoff placement header is invalid")
    packed_clusters = packed.get("clusters")
    placement_clusters = placement.get("clusters")
    if not isinstance(packed_clusters, Sequence) or not isinstance(
        placement_clusters, Sequence
    ):
        raise ValidationError("Xilinx physical handoff clusters are invalid")
    placement_by_id = {
        entry.get("cluster"): entry for entry in placement_clusters
        if isinstance(entry, Mapping)
    }
    if len(placement_by_id) != len(placement_clusters):
        raise ValidationError("Xilinx physical handoff placement IDs are invalid")

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(f"""
            PRAGMA application_id = {_APPLICATION_ID};
            PRAGMA user_version = {_USER_VERSION};
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = MEMORY;
            PRAGMA synchronous = FULL;
            CREATE TABLE manifest (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema TEXT NOT NULL,
                packed_clusters INTEGER NOT NULL,
                assignments INTEGER NOT NULL,
                cascade_chains INTEGER NOT NULL,
                constants INTEGER NOT NULL
            );
            CREATE TABLE document (
                name TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE cluster (
                ordinal INTEGER PRIMARY KEY,
                cluster_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                site_templates_json TEXT NOT NULL,
                control_set_json TEXT,
                control_sets_json TEXT,
                site_mode TEXT,
                site TEXT NOT NULL UNIQUE,
                site_type TEXT NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                fixed INTEGER NOT NULL CHECK(fixed IN (0, 1)),
                physical_region_json TEXT
            );
            CREATE TABLE assignment (
                cluster_ordinal INTEGER NOT NULL REFERENCES cluster(ordinal),
                instance TEXT NOT NULL UNIQUE,
                cell_type TEXT NOT NULL,
                bel TEXT NOT NULL,
                placement_mode TEXT NOT NULL,
                physical_site TEXT NOT NULL,
                packed_ordinal INTEGER NOT NULL,
                placement_ordinal INTEGER NOT NULL,
                PRIMARY KEY(cluster_ordinal, packed_ordinal)
            ) WITHOUT ROWID;
            CREATE INDEX assignment_placement_order
                ON assignment(cluster_ordinal, placement_ordinal);
            CREATE TABLE cascade (
                ordinal INTEGER PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE constant (
                ordinal INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
        """)
        packed_document = {
            key: value for key, value in packed.items()
            if key not in {"clusters", "cascade_chains", "unplaced_constants"}
        }
        placement_document = {
            key: value for key, value in placement.items() if key != "clusters"
        }
        placement_source = dict(placement_document.get("source", {}))
        placement_source.pop("packed_sha256", None)
        placement_document["source"] = placement_source
        connection.executemany(
            "INSERT INTO document(name, value_json) VALUES (?, ?)",
            (("packed", _json(packed_document)),
             ("placement", _json(placement_document))),
        )
        assignment_count = 0
        seen_placement_ids = set()
        for cluster_ordinal, packed_cluster in enumerate(packed_clusters):
            if not isinstance(packed_cluster, Mapping):
                raise ValidationError("Xilinx physical handoff packing cluster is invalid")
            cluster_id = packed_cluster.get("id")
            placed = placement_by_id.get(cluster_id)
            if not isinstance(cluster_id, str) or not isinstance(placed, Mapping):
                raise ValidationError("Xilinx physical handoff cluster coverage disagrees")
            seen_placement_ids.add(cluster_id)
            packed_assignments = packed_cluster.get("assignments")
            placed_assignments = placed.get("assignments")
            if not isinstance(packed_assignments, Sequence) or not isinstance(
                placed_assignments, Sequence
            ):
                raise ValidationError("Xilinx physical handoff assignments are invalid")
            placed_by_instance = {
                value.get("instance"): (ordinal, value)
                for ordinal, value in enumerate(placed_assignments)
                if isinstance(value, Mapping)
            }
            if len(placed_by_instance) != len(placed_assignments):
                raise ValidationError("Xilinx physical handoff placement ownership is invalid")
            connection.execute(
                "INSERT INTO cluster VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cluster_ordinal, cluster_id, packed_cluster.get("kind"),
                    _json(packed_cluster.get("site_templates")),
                    _json(packed_cluster.get("control_set")),
                    (_json(packed_cluster["control_sets"])
                     if "control_sets" in packed_cluster else None),
                    packed_cluster.get("site_mode"), placed.get("site"),
                    placed.get("site_type"), placed.get("x"), placed.get("y"),
                    int(bool(placed.get("fixed"))),
                    _json(placed.get("physical_region")),
                ),
            )
            for packed_ordinal, assignment in enumerate(packed_assignments):
                if not isinstance(assignment, Mapping):
                    raise ValidationError("Xilinx physical handoff assignment is invalid")
                instance = assignment.get("instance")
                placed_record = placed_by_instance.get(instance)
                if placed_record is None:
                    raise ValidationError("Xilinx physical handoff assignment coverage disagrees")
                placement_ordinal, placed_assignment = placed_record
                if (
                    placed_assignment.get("cell_type") != assignment.get("cell_type")
                    or placed_assignment.get("bel") != assignment.get("bel")
                ):
                    raise ValidationError("Xilinx physical handoff assignment identity disagrees")
                connection.execute(
                    "INSERT INTO assignment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cluster_ordinal, instance, assignment.get("cell_type"),
                        assignment.get("bel"), placed_assignment.get("placement_mode"),
                        placed_assignment.get("site"), packed_ordinal,
                        placement_ordinal,
                    ),
                )
                assignment_count += 1
            if len(packed_assignments) != len(placed_assignments):
                raise ValidationError("Xilinx physical handoff assignment count disagrees")
        if seen_placement_ids != set(placement_by_id):
            raise ValidationError("Xilinx physical handoff cluster coverage disagrees")
        cascades = packed.get("cascade_chains", [])
        constants = packed.get("unplaced_constants", [])
        connection.executemany(
            "INSERT INTO cascade(ordinal, value_json) VALUES (?, ?)",
            ((index, _json(value)) for index, value in enumerate(cascades)),
        )
        connection.executemany(
            "INSERT INTO constant(ordinal, name) VALUES (?, ?)",
            ((index, value) for index, value in enumerate(constants)),
        )
        connection.execute(
            "INSERT INTO manifest VALUES (1, ?, ?, ?, ?, ?)",
            (
                XILINX_PHYSICAL_HANDOFF_SCHEMA, len(packed_clusters),
                assignment_count, len(cascades), len(constants),
            ),
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, path)
    except (sqlite3.Error, OSError, TypeError, ValueError) as error:
        raise ValidationError("failed to publish Xilinx physical handoff") from error
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def validate_xilinx_physical_handoff(
    path: Path, *, integrity_check: bool = False,
) -> Dict[str, Any]:
    """Validate schema and row accounting, with opt-in SQLite diagnostics."""

    connection = _connect(path)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
            raise ValidationError("Xilinx physical handoff application ID is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != _USER_VERSION:
            raise ValidationError("Xilinx physical handoff version is invalid")
        if integrity_check:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValidationError(
                    "Xilinx physical handoff integrity check failed"
                )
        manifest = connection.execute("SELECT * FROM manifest WHERE singleton=1").fetchone()
        if manifest is None or manifest["schema"] != XILINX_PHYSICAL_HANDOFF_SCHEMA:
            raise ValidationError("Xilinx physical handoff schema is invalid")
        counts = {
            "packed_clusters": connection.execute("SELECT count(*) FROM cluster").fetchone()[0],
            "assignments": connection.execute("SELECT count(*) FROM assignment").fetchone()[0],
            "cascade_chains": connection.execute("SELECT count(*) FROM cascade").fetchone()[0],
            "constants": connection.execute("SELECT count(*) FROM constant").fetchone()[0],
        }
        if any(manifest[name] != count for name, count in counts.items()):
            raise ValidationError("Xilinx physical handoff row counts disagree")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValidationError("Xilinx physical handoff foreign keys disagree")
        documents = {
            row["name"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT * FROM document")
        }
        if (
            set(documents) != {"packed", "placement"}
            or documents["packed"].get("schema") != PACKED_SITE_NETLIST_SCHEMA
            or documents["placement"].get("schema") != XILINX_PLACEMENT_SCHEMA
        ):
            raise ValidationError("Xilinx physical handoff documents are invalid")
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise ValidationError("Xilinx physical handoff is corrupt or truncated") from error
    finally:
        connection.close()
    return {
        "schema": "emuflow.xilinx-physical-handoff-validation/v1",
        "status": "pass", "integrity_check": integrity_check, **counts,
        "physical_handoff_sha256": file_sha256(path),
    }


def open_xilinx_physical_handoff(
    path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Open relocatable lazy compatibility views over one physical handoff."""

    connection = _connect(path)
    try:
        manifest = connection.execute("SELECT * FROM manifest WHERE singleton=1").fetchone()
        if manifest is None or manifest["schema"] != XILINX_PHYSICAL_HANDOFF_SCHEMA:
            raise ValidationError("Xilinx physical handoff schema is invalid")
        documents = {
            row["name"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT * FROM document")
        }
        cascades = [
            json.loads(row[0]) for row in connection.execute(
                "SELECT value_json FROM cascade ORDER BY ordinal"
            )
        ]
        constants = [
            row[0] for row in connection.execute(
                "SELECT name FROM constant ORDER BY ordinal"
            )
        ]
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise ValidationError("Xilinx physical handoff metadata is invalid") from error
    finally:
        connection.close()
    if set(documents) != {"packed", "placement"}:
        raise ValidationError("Xilinx physical handoff documents are incomplete")
    identity = file_sha256(path)
    packed = dict(documents["packed"])
    placement = dict(documents["placement"])
    packed_source = dict(packed.get("source", {}))
    packed_source["physical_handoff_sha256"] = identity
    packed["source"] = packed_source
    packed["clusters"] = _ClusterSequence(
        path, placement=False, count=manifest["packed_clusters"]
    )
    packed["cascade_chains"] = cascades
    packed["unplaced_constants"] = constants
    placement_source = dict(placement.get("source", {}))
    placement_source["packed_sha256"] = identity
    placement_source["physical_handoff_sha256"] = identity
    placement["source"] = placement_source
    placement["clusters"] = _ClusterSequence(
        path, placement=True, count=manifest["packed_clusters"]
    )
    return packed, placement


def validate_xilinx_physical_handoff_pair(
    packed: Mapping[str, Any], placement: Mapping[str, Any]
) -> None:
    """Reject mixing lazy views from different physical handoff runs."""

    packed_source = packed.get("source")
    placement_source = placement.get("source")
    packed_identity = (
        packed_source.get("physical_handoff_sha256")
        if isinstance(packed_source, Mapping) else None
    )
    placement_identity = (
        placement_source.get("physical_handoff_sha256")
        if isinstance(placement_source, Mapping) else None
    )
    if packed_identity is None and placement_identity is None:
        return
    if (
        not isinstance(packed_identity, str)
        or not isinstance(placement_identity, str)
        or len(packed_identity) != 64
        or any(character not in "0123456789abcdef" for character in packed_identity)
        or packed_identity != placement_identity
    ):
        raise ValidationError(
            "packing and placement physical handoff runs disagree"
        )
