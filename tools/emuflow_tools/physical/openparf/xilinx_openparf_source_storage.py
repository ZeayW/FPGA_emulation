"""Indexed storage for the derived OpenPARF atomic-source contract.

The production source selector is intentionally small.  It binds the mapped
netlist and a typed SQLite sidecar containing only facts that are not already
owned by the mapped index: physical macro clusters, cascade records and
constant-cell identities.  Ordinary singleton clusters are derived lazily
from the mapped cell relation, so a million-cell partition is not copied into
another JSON document or Python object tree.
"""

from __future__ import annotations

from emuflow.common.paths import DATA_ROOT

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource, bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.common.json_io import file_sha256, write_json
from emuflow_tools.document_io import read_json
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.physical.xilinx_netlist import open_xilinx_mapped_index, is_xilinx_mapped_index
from emuflow.physical.xilinx_primitives import CONSTANT_TYPES
from emuflow.physical.xilinx_primitives import DUAL_OUTPUT_LUT_TYPE
from emuflow.physical.xilinx_primitives import LUT_TYPES


OPENPARF_ATOMIC_SOURCE_SCHEMA = "emuflow.openparf-atomic-source/v1"
OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA = (
    "emuflow.openparf-atomic-source-storage/v1"
)
OPENPARF_ATOMIC_SOURCE_APPLICATION_ID = 0x454D5541  # ASCII "EMUA"
OPENPARF_ATOMIC_SOURCE_USER_VERSION = 1
_STORE_PREFIX = "openparf-atomic-source."
_STORE_SUFFIX = ".sqlite"
_HARD_RESOURCES = {"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}
_SLICE_LUT_TYPES = LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE}
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version",
    "header", "counts", "mapped", "logical_sha256", "artifact",
    "store_bytes", "store_sha256",
}
_HEADER_KEYS = {"schema", "status", "top", "source", "policy", "summary"}
_COUNT_KEYS = {
    "physical_cells", "macro_clusters", "macro_assignments",
    "singleton_clusters", "cascade_chains", "constant_cells",
}
_MAPPED_KEYS = {"artifact", "bytes", "sha256"}
_SOURCE_KEYS = {"mapped_sha256"}
_POLICY_KEYS = {"provider", "dedicated_or_relative_constraints"}
_SINGLETON_CLUSTER_PREFIX = "atomic-source-"


_DDL = (DATA_ROOT / "schema/openparf_atomic_source.sql").read_text(encoding="utf-8")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _store_name(digest: str) -> str:
    return f"{_STORE_PREFIX}{digest}{_STORE_SUFFIX}"


def _validate_selector_envelope(value: Any) -> dict[str, Any]:
    """Validate the exact versioned control envelope, independent of paths."""

    if (
        not isinstance(value, dict)
        or set(value) != _SELECTOR_KEYS
        or value.get("schema") != OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
        or value.get("logical_schema") != OPENPARF_ATOMIC_SOURCE_SCHEMA
        or isinstance(value.get("application_id"), bool)
        or value.get("application_id") != OPENPARF_ATOMIC_SOURCE_APPLICATION_ID
        or isinstance(value.get("user_version"), bool)
        or value.get("user_version") != OPENPARF_ATOMIC_SOURCE_USER_VERSION
        or not isinstance(value.get("header"), dict)
        or set(value["header"]) != _HEADER_KEYS
        or not isinstance(value.get("counts"), dict)
        or set(value["counts"]) != _COUNT_KEYS
        or not isinstance(value.get("mapped"), dict)
        or set(value["mapped"]) != _MAPPED_KEYS
    ):
        raise ValidationError("OpenPARF atomic-source selector is malformed")
    header = value["header"]
    counts = value["counts"]
    source = header.get("source")
    policy = header.get("policy")
    summary = header.get("summary")
    if (
        header.get("schema") != OPENPARF_ATOMIC_SOURCE_SCHEMA
        or header.get("status") != "pass"
        or not isinstance(header.get("top"), str)
        or not header["top"]
        or not isinstance(source, dict)
        or set(source) != _SOURCE_KEYS
        or not _valid_digest(source.get("mapped_sha256"))
        or not isinstance(policy, dict)
        or set(policy) != _POLICY_KEYS
        or policy != {
            "provider": "mapped-singleton-atoms-no-site-packing-v1",
            "dedicated_or_relative_constraints": "fail-closed",
        }
        or not isinstance(summary, dict)
        or set(summary) not in (
            {"physical_atoms", "constant_cells"},
            {"physical_atoms", "constant_cells", "physical_macros"},
        )
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in summary.values()
        )
        or not _valid_digest(value.get("logical_sha256"))
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in counts.values()
        )
    ):
        raise ValidationError("OpenPARF atomic-source logical header is malformed")
    mapped = value["mapped"]
    mapped_artifact = mapped.get("artifact")
    mapped_bytes = mapped.get("bytes")
    if (
        not isinstance(mapped_artifact, str)
        or not mapped_artifact
        or Path(mapped_artifact).name != mapped_artifact
        or isinstance(mapped_bytes, bool)
        or not isinstance(mapped_bytes, int)
        or mapped_bytes < 0
        or not _valid_digest(mapped.get("sha256"))
    ):
        raise ValidationError("OpenPARF atomic-source mapped descriptor is invalid")
    artifact = value.get("artifact")
    store_sha = value.get("store_sha256")
    store_bytes = value.get("store_bytes")
    if (
        not isinstance(artifact, str)
        or not artifact
        or Path(artifact).name != artifact
        or not _valid_digest(store_sha)
        or artifact != _store_name(store_sha)
        or isinstance(store_bytes, bool)
        or not isinstance(store_bytes, int)
        or store_bytes < 0
    ):
        raise ValidationError("OpenPARF atomic-source artifact descriptor is invalid")
    return value


def _resolve_sealed_store(
    selector_path: Path, selector: Mapping[str, Any]
) -> tuple[Path, StoreSource]:
    """Resolve and byte-validate only the selector-owned immutable sidecar."""

    store_path = selector_path.resolve().with_name(str(selector["artifact"]))
    try:
        stat = store_path.stat()
        store_source = bind_store_source(store_path, "openparf-atomic-source-store-v1")
    except OSError as error:
        raise ValidationError("OpenPARF atomic-source store is missing") from error
    if (
        int(selector["store_bytes"]) != stat.st_size
        or file_sha256(store_path) != selector["store_sha256"]
    ):
        raise ValidationError("OpenPARF atomic-source store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return store_path, store_source


def _previous_source_store(path: Path) -> Optional[Path]:
    """Discover an old sidecar without consulting the new mapped generation."""

    if not path.is_file():
        return None
    try:
        previous = read_json(path)
    except (OSError, ValueError) as error:
        raise ValidationError(
            "previous OpenPARF atomic-source selector is corrupt"
        ) from error
    if (
        isinstance(previous, Mapping)
        and previous.get("schema") == OPENPARF_ATOMIC_SOURCE_SCHEMA
    ):
        return None
    selector = _validate_selector_envelope(previous)
    return _resolve_sealed_store(path, selector)[0]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _ordered_cell_types(cells: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    typed = getattr(cells, "types_by_id", None)
    if callable(typed):
        yield from typed()
        return
    indexed = getattr(cells, "items_by_id", None)
    items = indexed() if callable(indexed) else sorted(cells.items())
    for name, cell in items:
        if not isinstance(cell, Mapping) or not isinstance(cell.get("type"), str):
            raise ValidationError("mapped JSON cell is invalid")
        yield str(name), str(cell["type"])


def _singleton_cluster(index: int, name: str, cell_type: str) -> dict[str, Any]:
    hard = cell_type in _HARD_RESOURCES
    hard_bel_candidates = (
        ["RAMB18E2_L", "RAMB18E2_U"]
        if cell_type == "RAMB18E2" else [cell_type]
    )
    return {
        "id": f"atomic-source-{index:06d}",
        "kind": "hard" if hard else "slice",
        "site_templates": [cell_type] if hard else ["SLICEL", "SLICEM"],
        "control_set": None,
        "assignments": [{
            "instance": name,
            "cell_type": cell_type,
            "bel_candidates": (
                hard_bel_candidates if hard
                else [f"{letter}6LUT" for letter in "ABCDEFGH"]
                if cell_type in _SLICE_LUT_TYPES
                else [
                    bel for letter in "ABCDEFGH"
                    for bel in (f"{letter}FF", f"{letter}FF2")
                ]
            ),
        }],
    }


class _StoredJsonSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, store_path: Path, table: str, count: int):
        self.store_path = store_path
        self.table = table
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                f"SELECT record_json FROM {self.table} ORDER BY seq"
            ):
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"OpenPARF atomic-source {self.table} record is invalid"
                    )
                observed += 1
                yield value
            if observed != self.count:
                raise ValidationError(
                    f"OpenPARF atomic-source {self.table} count disagrees"
                )
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"OpenPARF atomic-source {self.table} record is corrupt"
            ) from error
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                f"SELECT record_json FROM {self.table} WHERE seq=?", (index,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError(
                f"OpenPARF atomic-source {self.table} sequence is not contiguous"
            )
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValidationError(
                f"OpenPARF atomic-source {self.table} record is invalid"
            )
        return value


class _ConstantSequence(Sequence[str]):
    def __init__(self, store_path: Path, count: int):
        self.store_path = store_path
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[str]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT instance FROM constants ORDER BY seq"
            ):
                observed += 1
                yield str(row[0])
            if observed != self.count:
                raise ValidationError("OpenPARF atomic-source constant count disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                "SELECT instance FROM constants WHERE seq=?", (index,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError(
                "OpenPARF atomic-source constant sequence is not contiguous"
            )
        return str(row[0])

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return list(self) == list(other)
        return False


class _AtomicClusterSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        cells: Mapping[str, Any],
        macro_count: int,
        singleton_count: int,
    ):
        self.store_path = store_path
        self.cells = cells
        self.macro_count = macro_count
        self.singleton_count = singleton_count

    def __len__(self) -> int:
        return self.macro_count + self.singleton_count

    @staticmethod
    def _decode_cluster(row: sqlite3.Row, assignments: list[dict[str, Any]]) -> dict[str, Any]:
        cluster = {
            "id": str(row["cluster_id"]),
            "kind": str(row["kind"]),
            "site_templates": json.loads(row["site_templates_json"]),
            "control_set": json.loads(row["control_set_json"]),
            "assignments": assignments,
        }
        if row["site_mode"] is not None:
            cluster["site_mode"] = str(row["site_mode"])
        return cluster

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            assignment_rows = iter(connection.execute(
                "SELECT * FROM assignments ORDER BY cluster_seq,seq"
            ))
            assignment = next(assignment_rows, None)
            observed_macros = 0
            for row in connection.execute("SELECT * FROM clusters ORDER BY seq"):
                values: list[dict[str, Any]] = []
                while assignment is not None and int(assignment["cluster_seq"]) == int(row["seq"]):
                    value: dict[str, Any] = {
                        "instance": str(assignment["instance"]),
                        "cell_type": str(assignment["cell_type"]),
                        "bel_candidates": json.loads(
                            assignment["bel_candidates_json"]
                        ),
                    }
                    if assignment["bel"] is not None:
                        value["bel"] = str(assignment["bel"])
                    values.append(value)
                    assignment = next(assignment_rows, None)
                if not values:
                    raise ValidationError(
                        "OpenPARF atomic-source macro has no assignments"
                    )
                observed_macros += 1
                yield self._decode_cluster(row, values)
            if observed_macros != self.macro_count or assignment is not None:
                raise ValidationError(
                    "OpenPARF atomic-source macro relation disagrees"
                )

            owned_rows = iter(connection.execute(
                "SELECT instance FROM assignments ORDER BY instance"
            ))
            owned = next(owned_rows, None)
            observed_singletons = 0
            for index, (name, cell_type) in enumerate(
                _ordered_cell_types(self.cells)
            ):
                while owned is not None and str(owned[0]) < name:
                    raise ValidationError(
                        "OpenPARF atomic-source macro ownership is invalid"
                    )
                if owned is not None and str(owned[0]) == name:
                    owned = next(owned_rows, None)
                    continue
                if cell_type in CONSTANT_TYPES:
                    continue
                observed_singletons += 1
                yield _singleton_cluster(index, name, cell_type)
            if owned is not None or observed_singletons != self.singleton_count:
                raise ValidationError(
                    "OpenPARF atomic-source singleton relation disagrees"
                )
        except json.JSONDecodeError as error:
            raise ValidationError(
                "OpenPARF atomic-source macro record is corrupt"
            ) from error
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        from itertools import islice
        return next(islice(iter(self), index, index + 1))


class XilinxOpenparfAtomicSourceView(Mapping[str, Any]):
    """Checked lazy logical view over one compact source selector."""

    def __init__(
        self,
        selector_path: Path,
        *,
        mapped_path: Optional[Path] = None,
        mapped_value: Optional[Mapping[str, Any]] = None,
        mapped_sha256: Optional[str] = None,
    ):
        self.selector_path = selector_path.resolve()
        self.selector_generation = native_file_generation(self.selector_path)
        selector = _validate_selector_envelope(read_json(self.selector_path))
        header = selector["header"]
        counts_value = selector["counts"]
        self.store_path, store_source = _resolve_sealed_store(
            self.selector_path, selector
        )
        self.store_generation = store_source.generation

        mapped_descriptor = selector["mapped"]
        mapped_artifact = mapped_descriptor["artifact"]
        expected_mapped_sha = mapped_descriptor.get("sha256")
        self.mapped_path = (
            Path(mapped_path).resolve()
            if mapped_path is not None
            else self.selector_path.with_name(mapped_artifact)
        )
        self.mapped_schema = (
            "xilinx-mapped-index-v2"
            if is_xilinx_mapped_index(self.mapped_path)
            else "xilinx-mapped-json-v1"
        )
        try:
            self.mapped_generation = native_file_generation(self.mapped_path)
            mapped_stat = self.mapped_path.stat()
        except OSError as error:
            raise ValidationError("OpenPARF atomic-source mapped input is missing") from error
        if mapped_descriptor["bytes"] != mapped_stat.st_size:
            raise ValidationError("OpenPARF atomic-source mapped size disagrees")
        observed_mapped_sha = (
            mapped_sha256
            if mapped_sha256 is not None else file_sha256(self.mapped_path)
        )
        if observed_mapped_sha != expected_mapped_sha:
            raise ValidationError("OpenPARF atomic-source mapped seal disagrees")
        if (
            header.get("source") != {"mapped_sha256": expected_mapped_sha}
            or header.get("policy") != {
                "provider": "mapped-singleton-atoms-no-site-packing-v1",
                "dedicated_or_relative_constraints": "fail-closed",
            }
            or counts_value["physical_cells"]
            != counts_value["macro_assignments"]
            + counts_value["singleton_clusters"]
        ):
            raise ValidationError("OpenPARF atomic-source derivation contract disagrees")

        connection = _connect(self.store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != OPENPARF_ATOMIC_SOURCE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != OPENPARF_ATOMIC_SOURCE_USER_VERSION
            ):
                raise ValidationError("unsupported OpenPARF atomic-source store")
            metadata = {
                str(row[0]): json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            counts = {
                "macro_clusters": int(connection.execute(
                    "SELECT count(*) FROM clusters"
                ).fetchone()[0]),
                "macro_assignments": int(connection.execute(
                    "SELECT count(*) FROM assignments"
                ).fetchone()[0]),
                "cascade_chains": int(connection.execute(
                    "SELECT count(*) FROM cascades"
                ).fetchone()[0]),
                "constant_cells": int(connection.execute(
                    "SELECT count(*) FROM constants"
                ).fetchone()[0]),
            }
            if counts != {
                key: selector["counts"].get(key) for key in counts
            }:
                raise ValidationError("OpenPARF atomic-source row counts disagree")
            if set(metadata) != {"header", "counts", "mapped", "logical_sha256"}:
                raise ValidationError("OpenPARF atomic-source metadata keys disagree")
            if (
                metadata.get("header") != selector["header"]
                or metadata.get("counts") != selector["counts"]
                or metadata.get("mapped") != selector["mapped"]
                or metadata.get("logical_sha256") != selector.get("logical_sha256")
                or connection.execute(
                    "SELECT 1 FROM assignments AS a LEFT JOIN clusters AS c "
                    "ON c.seq=a.cluster_seq WHERE c.seq IS NULL LIMIT 1"
                ).fetchone() is not None
            ):
                raise ValidationError("OpenPARF atomic-source metadata disagrees")
            for table, expected in (
                ("clusters", counts["macro_clusters"]),
                ("cascades", counts["cascade_chains"]),
                ("constants", counts["constant_cells"]),
            ):
                minimum, maximum = connection.execute(
                    f"SELECT min(seq),max(seq) FROM {table}"
                ).fetchone()
                if (
                    (expected == 0 and (minimum is not None or maximum is not None))
                    or (
                        expected > 0
                        and (int(minimum) != 0 or int(maximum) != expected - 1)
                    )
                ):
                    raise ValidationError(
                        f"OpenPARF atomic-source {table} sequence is invalid"
                    )

            cluster_digest = hashlib.sha256()
            assignment_digest = hashlib.sha256()
            assignment_rows = iter(connection.execute(
                "SELECT * FROM assignments ORDER BY cluster_seq,seq"
            ))
            assignment = next(assignment_rows, None)
            for row in connection.execute("SELECT * FROM clusters ORDER BY seq"):
                assignments: list[dict[str, Any]] = []
                expected_assignment_seq = 0
                while (
                    assignment is not None
                    and int(assignment["cluster_seq"]) == int(row["seq"])
                ):
                    if int(assignment["seq"]) != expected_assignment_seq:
                        raise ValidationError(
                            "OpenPARF atomic-source assignment sequence is invalid"
                        )
                    item: dict[str, Any] = {
                        "instance": str(assignment["instance"]),
                        "cell_type": str(assignment["cell_type"]),
                        "bel_candidates": json.loads(
                            assignment["bel_candidates_json"]
                        ),
                    }
                    if assignment["bel"] is not None:
                        item["bel"] = str(assignment["bel"])
                    assignments.append(item)
                    assignment_digest.update(_canonical(item).encode("utf-8"))
                    assignment_digest.update(b"\n")
                    expected_assignment_seq += 1
                    assignment = next(assignment_rows, None)
                cluster: dict[str, Any] = {
                    "id": str(row["cluster_id"]),
                    "kind": str(row["kind"]),
                    "site_templates": json.loads(row["site_templates_json"]),
                    "control_set": json.loads(row["control_set_json"]),
                    "assignments": assignments,
                }
                if row["site_mode"] is not None:
                    cluster["site_mode"] = str(row["site_mode"])
                normalized, _ = _validate_macro_cluster(cluster)
                cluster_digest.update(_canonical(normalized).encode("utf-8"))
                cluster_digest.update(b"\n")
            if assignment is not None:
                raise ValidationError(
                    "OpenPARF atomic-source assignment ownership is invalid"
                )
            cascade_digest = hashlib.sha256()
            for row in connection.execute(
                "SELECT cascade_id,record_json FROM cascades ORDER BY seq"
            ):
                cascade = json.loads(row["record_json"])
                cascade_id = _validate_cascade_identity(row["cascade_id"])
                if (
                    not isinstance(cascade, dict)
                    or cascade.get("id") != cascade_id
                    or _canonical(cascade) != row["record_json"]
                    or connection.execute(
                        "SELECT 1 FROM clusters WHERE cluster_id=?", (cascade_id,)
                    ).fetchone() is not None
                ):
                    raise ValidationError(
                        "OpenPARF atomic-source cascade record disagrees"
                    )
                cascade_digest.update(row["record_json"].encode("utf-8"))
                cascade_digest.update(b"\n")
            constant_digest = hashlib.sha256()
            for row in connection.execute(
                "SELECT instance FROM constants ORDER BY seq"
            ):
                constant_digest.update(_canonical(str(row[0])).encode("utf-8"))
                constant_digest.update(b"\n")
            logical_sha = hashlib.sha256(_canonical({
                "header": selector["header"],
                "counts": selector["counts"],
                "mapped": selector["mapped"],
                "cluster_sha256": cluster_digest.hexdigest(),
                "assignment_sha256": assignment_digest.hexdigest(),
                "cascade_sha256": cascade_digest.hexdigest(),
                "constant_sha256": constant_digest.hexdigest(),
            }).encode("utf-8")).hexdigest()
            if logical_sha != selector["logical_sha256"]:
                raise ValidationError("OpenPARF atomic-source logical seal disagrees")
        except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
            raise ValidationError("OpenPARF atomic-source store is corrupt") from error
        finally:
            connection.close()

        if mapped_value is None:
            mapped_value = (
                open_xilinx_mapped_index(self.mapped_path)
                if is_xilinx_mapped_index(self.mapped_path)
                else read_json(self.mapped_path)
            )
        if not isinstance(mapped_value, Mapping):
            raise ValidationError("OpenPARF atomic-source mapped input is invalid")
        modules = mapped_value.get("modules")
        top = selector["header"].get("top")
        module = modules.get(top) if isinstance(modules, Mapping) else None
        cells = module.get("cells") if isinstance(module, Mapping) else None
        if not isinstance(cells, Mapping):
            raise ValidationError("OpenPARF atomic-source mapped top is invalid")
        self._mapped_owner = mapped_value
        self._cells = cells
        self._selector = selector
        self._header = dict(selector["header"])
        self._clusters = _AtomicClusterSequence(
            self.store_path,
            cells,
            int(selector["counts"]["macro_clusters"]),
            int(selector["counts"]["singleton_clusters"]),
        )
        self._cascades = _StoredJsonSequence(
            self.store_path, "cascades",
            int(selector["counts"]["cascade_chains"]),
        )
        self._constants = _ConstantSequence(
            self.store_path, int(selector["counts"]["constant_cells"])
        )
        self._validate_mapped_ownership()
        self.require_sources_unchanged()

    @classmethod
    def _from_native_publication(cls, selector_path, mapped_path, store, *, mapped_value=None, mapped_sha256=None, receipt):
        """Attach lazy readers to native-validated records without host rederivation."""
        value = cls.__new__(cls)
        value.selector_path = Path(selector_path).resolve()
        fields = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds", "ctime_seconds", "ctime_nanoseconds")
        def generation(record):
            return tuple(record["generation"][key] for key in fields)
        value.selector_generation = generation(receipt["outputs"][0])
        if native_file_generation(value.selector_path) != value.selector_generation:
            raise ValidationError("native OpenPARF selector generation changed")
        selector = _validate_selector_envelope(read_json(value.selector_path))
        value.store_path = value.selector_path.with_name(selector["artifact"])
        if value.store_path.resolve() != Path(store["path"]).resolve():
            raise ValidationError("native OpenPARF source output identity disagrees")
        value.store_generation = generation(store)
        value.mapped_path = Path(mapped_path).resolve()
        mapped_records = [r for r in receipt["inputs"] if Path(r["path"]).resolve() == value.mapped_path]
        if len(mapped_records) != 1:
            raise ValidationError("native OpenPARF mapped receipt disagrees")
        value.mapped_generation = generation(mapped_records[0])
        value.mapped_schema = "xilinx-mapped-index-v2" if is_xilinx_mapped_index(value.mapped_path) else "xilinx-mapped-json-v1"
        if mapped_sha256 is not None and mapped_sha256 != selector["mapped"]["sha256"]:
            raise ValidationError("OpenPARF atomic-source mapped seal disagrees")
        if mapped_value is None:
            mapped_value = open_xilinx_mapped_index(value.mapped_path) if value.mapped_schema == "xilinx-mapped-index-v2" else read_json(value.mapped_path)
        if value.mapped_schema == "xilinx-mapped-index-v2" and Path(mapped_value.path).resolve() != value.mapped_path:
            raise ValidationError("native OpenPARF mapped view belongs to another artifact")
        cells = mapped_value["modules"][selector["header"]["top"]]["cells"]
        value._mapped_owner, value._cells = mapped_value, cells
        value._selector, value._header = selector, dict(selector["header"])
        counts = selector["counts"]
        value._clusters = _AtomicClusterSequence(value.store_path, cells, counts["macro_clusters"], counts["singleton_clusters"])
        value._cascades = _StoredJsonSequence(value.store_path, "cascades", counts["cascade_chains"])
        value._constants = _ConstantSequence(value.store_path, counts["constant_cells"])
        value.require_sources_unchanged()
        return value

    def bound_store_sources(self):
        self.require_sources_unchanged()
        return (
            StoreSource(
                self.selector_path,
                "openparf-atomic-source-selector-v1",
                self.selector_generation,
            ),
            StoreSource(
                self.store_path,
                "openparf-atomic-source-store-v1",
                self.store_generation,
            ),
            StoreSource(self.mapped_path, self.mapped_schema, self.mapped_generation),
        )

    def require_sources_unchanged(self) -> None:
        try:
            for path, generation in (
                (self.selector_path, self.selector_generation),
                (self.store_path, self.store_generation),
                (self.mapped_path, self.mapped_generation),
            ):
                if native_file_generation(path) != generation:
                    raise ValidationError("OpenPARF atomic source generation changed")
            require = getattr(self._mapped_owner, "require_source_unchanged", None)
            if callable(require):
                require()
        except OSError as error:
            raise ValidationError("OpenPARF atomic source generation changed") from error

    def _validate_mapped_ownership(self) -> None:
        connection = _connect(self.store_path)
        try:
            owned_rows = iter(connection.execute(
                "SELECT instance,cell_type FROM assignments ORDER BY instance"
            ))
            owned = next(owned_rows, None)
            constants = iter(connection.execute(
                "SELECT instance FROM constants ORDER BY instance"
            ))
            constant = next(constants, None)
            physical = 0
            singleton = 0
            for name, cell_type in _ordered_cell_types(self._cells):
                if owned is not None and str(owned[0]) < name:
                    raise ValidationError(
                        "OpenPARF atomic-source macro cell is absent from mapped input"
                    )
                if constant is not None and str(constant[0]) < name:
                    raise ValidationError(
                        "OpenPARF atomic-source constant is absent from mapped input"
                    )
                if cell_type in CONSTANT_TYPES:
                    if constant is None or str(constant[0]) != name:
                        raise ValidationError(
                            "OpenPARF atomic-source constant coverage is incomplete"
                        )
                    constant = next(constants, None)
                    continue
                physical += 1
                if owned is not None and str(owned[0]) == name:
                    if str(owned[1]) != cell_type:
                        raise ValidationError(
                            "OpenPARF atomic-source macro cell type disagrees"
                        )
                    owned = next(owned_rows, None)
                else:
                    singleton += 1
            if owned is not None or constant is not None:
                raise ValidationError(
                    "OpenPARF atomic-source mapped ownership is invalid"
                )
            counts = self._selector["counts"]
            summary = self._header.get("summary")
            expected_summary: dict[str, Any] = {
                "physical_atoms": len(self._clusters),
                "constant_cells": len(self._constants),
            }
            if counts["macro_clusters"]:
                expected_summary["physical_macros"] = counts["macro_clusters"]
            if (
                singleton != counts.get("singleton_clusters")
                or physical != counts.get("physical_cells")
                or summary != expected_summary
            ):
                raise ValidationError("OpenPARF atomic-source summary disagrees")
        finally:
            connection.close()

    def __getitem__(self, key: str) -> Any:
        if key == "clusters":
            return self._clusters
        if key == "cascade_chains":
            return self._cascades
        if key == "unplaced_constants":
            return self._constants
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield "clusters"
        yield "cascade_chains"
        yield "unplaced_constants"

    def __len__(self) -> int:
        return len(self._header) + 3

    @property
    def selector(self) -> Mapping[str, Any]:
        return self._selector

    def iter_source_assignments(self) -> Iterator[tuple[str, str, str]]:
        """Stream ``(instance, source cluster, cell type)`` in identity order."""

        connection = _connect(self.store_path)
        try:
            owned_rows = iter(connection.execute(
                """
                SELECT a.instance,c.cluster_id,a.cell_type
                  FROM assignments AS a
                  JOIN clusters AS c ON c.seq=a.cluster_seq
                 ORDER BY a.instance
                """
            ))
            owned = next(owned_rows, None)
            observed = 0
            for index, (name, cell_type) in enumerate(
                _ordered_cell_types(self._cells)
            ):
                if owned is not None and str(owned["instance"]) < name:
                    raise ValidationError(
                        "OpenPARF atomic-source macro ownership is invalid"
                    )
                if cell_type in CONSTANT_TYPES:
                    continue
                if owned is not None and str(owned["instance"]) == name:
                    if str(owned["cell_type"]) != cell_type:
                        raise ValidationError(
                            "OpenPARF atomic-source macro cell type disagrees"
                        )
                    cluster_id = str(owned["cluster_id"])
                    owned = next(owned_rows, None)
                else:
                    cluster_id = f"atomic-source-{index:06d}"
                observed += 1
                yield name, cluster_id, cell_type
            if owned is not None or observed != self._selector["counts"]["physical_cells"]:
                raise ValidationError(
                    "OpenPARF atomic-source assignment coverage disagrees"
                )
        finally:
            connection.close()

    def materialize(self) -> dict[str, Any]:
        return {
            **self._header,
            "clusters": [dict(item) for item in self._clusters],
            "cascade_chains": [dict(item) for item in self._cascades],
            "unplaced_constants": list(self._constants),
        }








def read_xilinx_openparf_atomic_source(
    path: Path,
    *,
    mapped_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
) -> Mapping[str, Any]:
    """Read a typed selector, or explicitly materialize a legacy source JSON."""

    value = read_json(path)
    if (
        isinstance(value, Mapping)
        and value.get("schema") == OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
    ):
        return XilinxOpenparfAtomicSourceView(
            path, mapped_path=mapped_path, mapped_value=mapped_value,
            mapped_sha256=mapped_sha256,
        )
    if not isinstance(value, Mapping) or value.get("schema") != OPENPARF_ATOMIC_SOURCE_SCHEMA:
        raise ValidationError("OpenPARF atomic source header is invalid")
    return value


def materialize_xilinx_openparf_atomic_source(
    source_path: Path, output_path: Path
) -> dict[str, Any]:
    """Explicitly export the historical graph-sized JSON compatibility form."""

    source = read_xilinx_openparf_atomic_source(source_path)
    value = (
        source.materialize()
        if isinstance(source, XilinxOpenparfAtomicSourceView)
        else dict(source)
    )
    write_json(output_path, value, compact=True)
    return value


def _validate_macro_cluster(cluster: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cluster_id = cluster.get("id")
    kind = cluster.get("kind")
    templates = cluster.get("site_templates")
    assignments = cluster.get("assignments")
    if (
        not isinstance(cluster_id, str) or not cluster_id
        or cluster_id.startswith(_SINGLETON_CLUSTER_PREFIX)
        or kind not in {"slice", "carry", "hard"}
        or not isinstance(templates, list) or not templates
        or any(not isinstance(item, str) or not item for item in templates)
        or not isinstance(assignments, list) or not assignments
    ):
        raise ValidationError("OpenPARF atomic-source macro cluster is invalid")
    normalized_assignments = []
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise ValidationError("OpenPARF atomic-source macro assignment is invalid")
        instance = assignment.get("instance")
        cell_type = assignment.get("cell_type")
        candidates = assignment.get("bel_candidates")
        bel = assignment.get("bel")
        if (
            not isinstance(instance, str) or not instance
            or not isinstance(cell_type, str) or not cell_type
            or not isinstance(candidates, list) or not candidates
            or any(not isinstance(item, str) or not item for item in candidates)
            or (bel is not None and (not isinstance(bel, str) or not bel))
        ):
            raise ValidationError("OpenPARF atomic-source macro assignment is invalid")
        normalized = {
            "instance": instance, "cell_type": cell_type,
            "bel_candidates": list(candidates),
        }
        if bel is not None:
            normalized["bel"] = bel
        normalized_assignments.append(normalized)
    normalized_cluster = {
        "id": cluster_id, "kind": kind,
        "site_templates": list(templates),
        "control_set": cluster.get("control_set"),
        "assignments": normalized_assignments,
    }
    if cluster.get("site_mode") is not None:
        normalized_cluster["site_mode"] = cluster["site_mode"]
    return normalized_cluster, normalized_assignments



def _validate_cascade_identity(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(_SINGLETON_CLUSTER_PREFIX)
    ):
        raise ValidationError("OpenPARF atomic-source cascade identity is invalid")
    return value
