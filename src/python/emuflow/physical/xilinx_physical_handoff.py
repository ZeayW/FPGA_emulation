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

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource, bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.common.io import file_sha256
from emuflow.common.native_tools import native_file_generation
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.physical.xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from emuflow.physical.xilinx_placement import XILINX_PLACEMENT_SCHEMA


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
        source_binding: Optional[StoreSource] = None,
    ) -> None:
        self._path = path
        self._placement = placement
        self._count = count
        self._canonical_order = canonical_order
        self._source_binding = source_binding or bind_store_source(path, "xilinx-physical-handoff-v1")

    def bound_store_sources(self):
        require_store_sources_unchanged((self._source_binding,))
        return (self._source_binding,)

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self.bound_store_sources()
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
            self.bound_store_sources()

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

    def __init__(self, path: Path, *, sources: Iterable[Path] = ()) -> None:
        self.path = path
        self._bound_sources: Dict[Path, tuple[int, ...]] = {}
        self._typed_sources = ()
        self.bind_sources(sources)
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

    def bind_sources(self, paths: Iterable[Path]) -> None:
        """Bind source generations before reading or deriving their records."""

        self._require_sources_unchanged()
        for value in paths:
            path = Path(value).absolute()
            try:
                if self.path.resolve() == path.resolve() or (
                    self.path.exists() and os.path.samefile(self.path, path)
                ):
                    raise ValidationError("Xilinx physical handoff output aliases an input")
                generation = native_file_generation(path)
            except OSError as error:
                raise ValidationError("Xilinx physical handoff source cannot be bound") from error
            if path in self._bound_sources and self._bound_sources[path] != generation:
                raise ValidationError("Xilinx physical handoff source generation changed")
            self._bound_sources[path] = generation

    def bind_store_sources(self, sources):
        sources = validate_store_destination(self.path, sources)
        self._require_sources_unchanged()
        for source in sources:
            old = self._bound_sources.get(source.path)
            if old is not None and old != source.generation:
                raise ValidationError(
                    "Xilinx physical handoff source generation changed"
                )
            self._bound_sources[source.path] = source.generation
        existing = {
            (source.path, source.schema): source for source in self._typed_sources
        }
        for source in sources:
            old = existing.get((source.path, source.schema))
            if old is not None and old.generation != source.generation:
                raise ValidationError(
                    "Xilinx physical handoff source generation changed"
                )
            existing[(source.path, source.schema)] = source
        self._typed_sources = tuple(existing.values())

    def _require_sources_unchanged(self) -> None:
        require_store_sources_unchanged(self._typed_sources)
        for path, expected in self._bound_sources.items():
            try:
                if native_file_generation(path) != expected:
                    raise ValidationError("Xilinx physical handoff source generation changed")
                if self.path.resolve() == path.resolve() or (
                    self.path.exists() and os.path.samefile(self.path, path)
                ):
                    raise ValidationError("Xilinx physical handoff output aliases an input")
            except OSError as error:
                raise ValidationError("Xilinx physical handoff source generation changed") from error

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
        self._require_sources_unchanged()
        try:
            sources = (
                tuple(
                    StoreSource(
                        path.resolve(), "physical-input-document-v1", generation
                    )
                    for path, generation in self._bound_sources.items()
                )
                + self._typed_sources
            )
            publish_sqlite_store(
                self.temporary_path,
                self.path,
                "xilinx-physical-handoff-v1",
                sources=sources,
            )
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

    source_binding = bind_store_source(path, "xilinx-physical-handoff-v1")
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
    require_store_sources_unchanged((source_binding,))
    packed = dict(documents["packed"])
    placement = dict(documents["placement"])
    packed_source = dict(packed.get("source", {}))
    packed_source["physical_handoff_sha256"] = identity
    packed["source"] = packed_source
    packed["clusters"] = _ClusterSequence(
        path, placement=False, count=manifest["packed_clusters"],
        canonical_order=canonical_order,
        source_binding=source_binding,
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
        source_binding=source_binding,
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
