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
from typing import Any, Dict, Iterable, Optional

from .errors import ValidationError
from .io import file_sha256
from .sqlite_artifacts import immutable_sqlite_uri
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_PLACEMENT_SCHEMA


XILINX_PHYSICAL_HANDOFF_SCHEMA = "emuflow.xilinx-physical-handoff-sqlite/v1"
_APPLICATION_ID = 0x454D5550  # EMUP
_USER_VERSION = 1
_BUILDER_COMMIT_ROWS = 65_536
_CANONICAL_CLUSTER_ORDER_FEATURE = "canonical-cluster-id-order-v1"


_HANDOFF_DDL = f"""
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
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(immutable_sqlite_uri(path), uri=True)
    except sqlite3.Error as error:
        raise ValidationError("Xilinx physical handoff cannot be opened") from error
    connection.row_factory = sqlite3.Row
    return connection


class _ClusterSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self, path: Path, *, placement: bool, count: int,
        canonical_order: bool = False,
    ) -> None:
        self._path = path
        self._placement = placement
        self._count = count
        self._canonical_order = canonical_order

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self._path)
        try:
            if self._canonical_order:
                cluster_rows = connection.execute(
                    "SELECT * FROM cluster ORDER BY cluster_id"
                )
                assignment_rows = iter(connection.execute(
                    "SELECT a.* FROM assignment a JOIN cluster c "
                    "ON c.ordinal=a.cluster_ordinal ORDER BY c.cluster_id, "
                    + (
                        "a.placement_ordinal"
                        if self._placement else "a.packed_ordinal"
                    )
                ))
            else:
                cluster_rows = connection.execute(
                    "SELECT * FROM cluster ORDER BY ordinal"
                )
                assignment_rows = iter(connection.execute(
                    "SELECT * FROM assignment ORDER BY cluster_ordinal, "
                    + (
                        "placement_ordinal"
                        if self._placement else "packed_ordinal"
                    )
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


def iter_xilinx_physical_handoff_cluster_pairs(
    path: Path,
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Read packing and placement together with one assignment-table scan."""

    connection = _connect(path)
    try:
        cluster_rows = connection.execute(
            "SELECT * FROM cluster ORDER BY cluster_id"
        )
        assignment_rows = iter(connection.execute(
            "SELECT a.* FROM assignment a JOIN cluster c "
            "ON c.ordinal=a.cluster_ordinal "
            "ORDER BY c.cluster_id, a.packed_ordinal"
        ))
        assignment = next(assignment_rows, None)
        for row in cluster_rows:
            rows = []
            while (
                assignment is not None
                and assignment["cluster_ordinal"] == row["ordinal"]
            ):
                rows.append(assignment)
                assignment = next(assignment_rows, None)
            packed_assignments = [
                {
                    "instance": item["instance"],
                    "cell_type": item["cell_type"],
                    "bel": item["bel"],
                }
                for item in rows
            ]
            placed_assignments = [
                {
                    "instance": item["instance"],
                    "cell_type": item["cell_type"],
                    "bel": item["bel"],
                    "placement_mode": item["placement_mode"],
                    "site": item["physical_site"],
                }
                for item in sorted(rows, key=lambda item: item["placement_ordinal"])
            ]
            packed: Dict[str, Any] = {
                "id": row["cluster_id"], "kind": row["kind"],
                "site_templates": _decode(row["site_templates_json"]),
                "control_set": _decode(row["control_set_json"]),
                "assignments": packed_assignments,
            }
            control_sets = _decode(row["control_sets_json"])
            if control_sets is not None:
                packed["control_sets"] = control_sets
            if row["site_mode"] is not None:
                packed["site_mode"] = row["site_mode"]
            yield packed, {
                "cluster": row["cluster_id"], "site": row["site"],
                "site_type": row["site_type"], "x": row["x"],
                "y": row["y"], "fixed": bool(row["fixed"]),
                "physical_region": _decode(row["physical_region_json"]),
                "assignments": placed_assignments,
            }
        if assignment is not None:
            raise ValidationError(
                "Xilinx physical handoff contains orphan assignments"
            )
    except sqlite3.Error as error:
        raise ValidationError("Xilinx physical handoff rows are invalid") from error
    finally:
        connection.close()


class XilinxPhysicalHandoffBuilder:
    """Build one physical handoff incrementally and publish it explicitly.

    The builder owns a same-directory temporary SQLite file.  Callers may
    stream validated bridge clusters into it, seal the metadata, independently
    validate lazy views over ``temporary_path``, and only then call
    :meth:`publish`.  ``close`` removes every unpublished temporary artifact.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        self.temporary_path = Path(temporary_name)
        self._connection: Optional[sqlite3.Connection] = None
        self._clusters = 0
        self._assignments = 0
        self._sealed = False
        self._published = False
        self._rows_since_commit = 0
        self._source_path: Optional[Path] = None
        self._source_connection: Optional[sqlite3.Connection] = None
        self._source_rows_since_commit = 0
        try:
            self._connection = sqlite3.connect(self.temporary_path)
            self._connection.executescript(_HANDOFF_DDL)
            self._connection.execute(
                "CREATE TABLE feature (name TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            self._connection.execute(
                "INSERT INTO feature(name) VALUES (?)",
                (_CANONICAL_CLUSTER_ORDER_FEATURE,),
            )
        except (sqlite3.Error, OSError) as error:
            self.close()
            raise ValidationError(
                "failed to initialize Xilinx physical handoff"
            ) from error

    def append_bridge_cluster(self, cluster: Mapping[str, Any]) -> None:
        """Append one normalized bridge-owned cluster without retaining it."""

        if self._sealed or self._connection is None:
            raise ValidationError("Xilinx physical handoff builder is sealed")
        assignments = cluster.get("assignments")
        placement = cluster.get("_placement")
        if (
            not isinstance(assignments, Sequence)
            or isinstance(assignments, (str, bytes))
            or not isinstance(placement, Mapping)
        ):
            raise ValidationError("Xilinx physical handoff bridge cluster is invalid")
        ordinal = self._clusters
        try:
            self._connection.execute(
                "INSERT INTO cluster VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ordinal, cluster.get("id"), cluster.get("kind"),
                    _json(cluster.get("site_templates")),
                    _json(cluster.get("control_set")),
                    (_json(cluster["control_sets"])
                     if "control_sets" in cluster else None),
                    cluster.get("site_mode"), placement.get("site"),
                    placement.get("site_type"), placement.get("x"),
                    placement.get("y"), int(bool(placement.get("fixed"))),
                    _json(placement.get("physical_region")),
                ),
            )
            self._rows_since_commit += 1
            for packed_ordinal, assignment in enumerate(assignments):
                if not isinstance(assignment, Mapping):
                    raise ValidationError(
                        "Xilinx physical handoff bridge assignment is invalid"
                    )
                self._connection.execute(
                    "INSERT INTO assignment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ordinal, assignment.get("instance"),
                        assignment.get("cell_type"), assignment.get("bel"),
                        assignment.get("_placement_mode"),
                        assignment.get("_physical_site"), packed_ordinal,
                        assignment.get("_placement_ordinal"),
                    ),
                )
                source_cluster = assignment.get("_source_cluster")
                if source_cluster is not None:
                    if self._source_connection is None:
                        raise ValidationError(
                            "OpenPARF atomic source cell ownership is invalid"
                        )
                    self._source_connection.execute(
                        "INSERT INTO certificate(instance, cluster_id) VALUES (?, ?)",
                        (assignment.get("instance"), source_cluster),
                    )
                    self._source_rows_since_commit += 1
                    if self._source_rows_since_commit >= _BUILDER_COMMIT_ROWS:
                        self._source_connection.commit()
                        self._source_rows_since_commit = 0
                self._assignments += 1
                self._rows_since_commit += 1
            if self._rows_since_commit >= _BUILDER_COMMIT_ROWS:
                # The file is private until publish, so committing bounded
                # batches cannot expose partial state.  It prevents an
                # in-memory rollback journal from growing with the design.
                self._connection.commit()
                self._rows_since_commit = 0
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise ValidationError(
                "failed to append Xilinx physical handoff cluster"
            ) from error
        self._clusters += 1

    def append_source_assignment(self, instance: str, cluster_id: str) -> None:
        """Stage source ownership on disk for a later relational comparison."""

        if self._sealed:
            raise ValidationError("Xilinx physical handoff builder is sealed")
        try:
            if self._source_connection is None:
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.source.", suffix=".tmp",
                    dir=self.path.parent,
                )
                os.close(descriptor)
                self._source_path = Path(name)
                self._source_connection = sqlite3.connect(self._source_path)
                self._source_connection.execute(
                    "CREATE TABLE ownership ("
                    "instance TEXT PRIMARY KEY, cluster_id TEXT NOT NULL"
                    ") WITHOUT ROWID"
                )
                self._source_connection.execute(
                    "CREATE TABLE certificate ("
                    "instance TEXT PRIMARY KEY, cluster_id TEXT NOT NULL"
                    ") WITHOUT ROWID"
                )
            self._source_connection.execute(
                "INSERT INTO ownership(instance, cluster_id) VALUES (?, ?)",
                (instance, cluster_id),
            )
            self._source_rows_since_commit += 1
            if self._source_rows_since_commit >= _BUILDER_COMMIT_ROWS:
                self._source_connection.commit()
                self._source_rows_since_commit = 0
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "OpenPARF atomic source cell ownership is invalid"
            ) from error
        except (sqlite3.Error, OSError) as error:
            raise ValidationError(
                "failed to stage OpenPARF atomic source ownership"
            ) from error

    def validate_source_ownership(self, expected_count: int) -> None:
        """Compare source and certificate ownership without Python owner maps."""

        if self._connection is None or self._sealed:
            raise ValidationError("Xilinx physical handoff builder is sealed")
        try:
            if self._source_connection is None:
                return
            assert self._source_path is not None
            self._source_connection.commit()
            source_count, certificate_count = self._source_connection.execute(
                "SELECT (SELECT count(*) FROM ownership), "
                "(SELECT count(*) FROM certificate)"
            ).fetchone()
            if source_count != expected_count or certificate_count != expected_count:
                raise ValidationError(
                    "OpenPARF atomic source cell coverage is incomplete"
                )
            cursor = self._source_connection.execute(
                "SELECT 1 FROM certificate c LEFT JOIN ownership s "
                "ON s.instance=c.instance WHERE s.instance IS NULL "
                "OR c.cluster_id IS NOT s.cluster_id "
                "UNION ALL "
                "SELECT 1 FROM ownership s LEFT JOIN certificate c "
                "ON c.instance=s.instance WHERE c.instance IS NULL LIMIT 1"
            )
            mismatch = cursor.fetchone()
            cursor.close()
            if mismatch is not None:
                raise ValidationError(
                    "OpenPARF atomic source cell ownership is invalid"
                )
        except sqlite3.Error as error:
            raise ValidationError(
                "failed to validate OpenPARF atomic source ownership"
            ) from error

    def update_assignment_bels(
        self, clusters: Iterable[Mapping[str, Any]]
    ) -> None:
        """Persist bounded in-site legalization edits before sealing."""

        if self._sealed or self._connection is None:
            raise ValidationError("Xilinx physical handoff builder is sealed")
        try:
            for cluster in clusters:
                for assignment in cluster.get("assignments", []):
                    cursor = self._connection.execute(
                        "UPDATE assignment SET bel=? WHERE instance=?",
                        (assignment.get("bel"), assignment.get("instance")),
                    )
                    if cursor.rowcount != 1:
                        raise ValidationError(
                            "Xilinx physical handoff legalization coverage disagrees"
                        )
                    self._rows_since_commit += 1
                    if self._rows_since_commit >= _BUILDER_COMMIT_ROWS:
                        self._connection.commit()
                        self._rows_since_commit = 0
        except sqlite3.Error as error:
            raise ValidationError(
                "failed to update Xilinx physical handoff legalization"
            ) from error

    def seal(
        self,
        packed: Mapping[str, Any],
        placement: Mapping[str, Any],
        *,
        cascades: Sequence[Mapping[str, Any]],
        constants: Sequence[str],
    ) -> None:
        """Finalize documents and counts while keeping the file unpublished."""

        if self._sealed or self._connection is None:
            raise ValidationError("Xilinx physical handoff builder is sealed")
        if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
            raise ValidationError("Xilinx physical handoff packing header is invalid")
        if placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
            raise ValidationError("Xilinx physical handoff placement header is invalid")
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
        try:
            self._connection.executemany(
                "INSERT INTO document(name, value_json) VALUES (?, ?)",
                (("packed", _json(packed_document)),
                 ("placement", _json(placement_document))),
            )
            self._connection.executemany(
                "INSERT INTO cascade(ordinal, value_json) VALUES (?, ?)",
                ((index, _json(value)) for index, value in enumerate(cascades)),
            )
            self._connection.executemany(
                "INSERT INTO constant(ordinal, name) VALUES (?, ?)",
                ((index, value) for index, value in enumerate(constants)),
            )
            self._connection.execute(
                "INSERT INTO manifest VALUES (1, ?, ?, ?, ?, ?)",
                (
                    XILINX_PHYSICAL_HANDOFF_SCHEMA, self._clusters,
                    self._assignments, len(cascades), len(constants),
                ),
            )
            self._connection.commit()
            self._connection.close()
            self._connection = None
            self._sealed = True
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise ValidationError("failed to seal Xilinx physical handoff") from error

    def publish(self) -> None:
        """Atomically replace the destination after caller-side validation."""

        if not self._sealed or self._published:
            raise ValidationError("Xilinx physical handoff is not ready to publish")
        try:
            os.replace(self.temporary_path, self.path)
        except OSError as error:
            raise ValidationError("failed to publish Xilinx physical handoff") from error
        self._published = True

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if not self._published:
            self.temporary_path.unlink(missing_ok=True)
        if self._source_connection is not None:
            self._source_connection.close()
            self._source_connection = None
        if self._source_path is not None:
            self._source_path.unlink(missing_ok=True)
            self._source_path = None

    def __enter__(self) -> "XilinxPhysicalHandoffBuilder":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


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
    # The production OpenPARF bridge sorts both views by cluster identity.
    # Preserve support for compatibility callers with arbitrary placement
    # order, but avoid a graph-sized duplicate cluster dictionary in the
    # ordered production path.
    placements_are_aligned = (
        len(packed_clusters) == len(placement_clusters)
        and all(
            isinstance(packed_cluster, Mapping)
            and isinstance(placed_cluster, Mapping)
            and packed_cluster.get("id") == placed_cluster.get("cluster")
            for packed_cluster, placed_cluster in zip(
                packed_clusters, placement_clusters
            )
        )
    )
    placement_by_id: Optional[Dict[str, Mapping[str, Any]]] = None
    if not placements_are_aligned:
        placement_by_id = {
            entry.get("cluster"): entry for entry in placement_clusters
            if isinstance(entry, Mapping)
            and isinstance(entry.get("cluster"), str)
        }
        if len(placement_by_id) != len(placement_clusters):
            raise ValidationError("Xilinx physical handoff placement IDs are invalid")
    placement_iterator = iter(placement_clusters) if placements_are_aligned else None

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(_HANDOFF_DDL)
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
        seen_placement_ids = set() if placement_by_id is not None else None
        for cluster_ordinal, packed_cluster in enumerate(packed_clusters):
            if not isinstance(packed_cluster, Mapping):
                raise ValidationError("Xilinx physical handoff packing cluster is invalid")
            cluster_id = packed_cluster.get("id")
            if placement_iterator is not None:
                placed = next(placement_iterator)
            else:
                assert placement_by_id is not None
                placed = placement_by_id.get(cluster_id)
            if not isinstance(cluster_id, str) or not isinstance(placed, Mapping):
                raise ValidationError("Xilinx physical handoff cluster coverage disagrees")
            if seen_placement_ids is not None:
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
        if placement_by_id is not None and (
            seen_placement_ids != set(placement_by_id)
        ):
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
        feature_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature'"
        ).fetchone()
        if feature_table is not None and {
            row[0] for row in connection.execute("SELECT name FROM feature")
        } != {_CANONICAL_CLUSTER_ORDER_FEATURE}:
            raise ValidationError("Xilinx physical handoff features are invalid")
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
    path: Path, *, validated_sha256: Optional[str] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Open relocatable lazy compatibility views over one physical handoff."""

    if validated_sha256 is not None and (
        len(validated_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in validated_sha256
        )
    ):
        raise ValidationError("Xilinx physical handoff digest is invalid")

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
        feature_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature'"
        ).fetchone()
        canonical_order = False
        if feature_table is not None:
            features = [
                row[0] for row in connection.execute("SELECT name FROM feature")
            ]
            if features != [_CANONICAL_CLUSTER_ORDER_FEATURE]:
                raise ValidationError(
                    "Xilinx physical handoff features are invalid"
                )
            canonical_order = True
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise ValidationError("Xilinx physical handoff metadata is invalid") from error
    finally:
        connection.close()
    if set(documents) != {"packed", "placement"}:
        raise ValidationError("Xilinx physical handoff documents are incomplete")
    # The production caller has just validated and hashed the immutable,
    # atomically published artifact.  Reusing that identity avoids a second
    # complete read of the same large database.  Standalone callers retain
    # the self-contained hash calculation.
    identity = validated_sha256 or file_sha256(path)
    packed = dict(documents["packed"])
    placement = dict(documents["placement"])
    packed_source = dict(packed.get("source", {}))
    packed_source["physical_handoff_sha256"] = identity
    packed["source"] = packed_source
    packed["clusters"] = _ClusterSequence(
        path, placement=False, count=manifest["packed_clusters"],
        canonical_order=canonical_order,
    )
    packed["cascade_chains"] = cascades
    packed["unplaced_constants"] = constants
    placement_source = dict(placement.get("source", {}))
    placement_source["packed_sha256"] = identity
    placement_source["physical_handoff_sha256"] = identity
    placement["source"] = placement_source
    placement["clusters"] = _ClusterSequence(
        path, placement=True, count=manifest["packed_clusters"],
        canonical_order=canonical_order,
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
