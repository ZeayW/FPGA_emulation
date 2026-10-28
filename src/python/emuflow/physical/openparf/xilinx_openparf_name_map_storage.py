"""Indexed storage for the OpenPARF atomic name-map contract.

The OpenPARF process itself consumes the Bookshelf files and never needs the
historical graph-sized ``name_map.json``.  Production EmuFlow consumers need
the same atom identities after placement, so keep those rows in a sealed typed
sidecar and leave the JSON path as a small commit selector.
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Iterable, Iterator, Mapping, Sequence
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Optional
import weakref

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource, bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.common.io import file_sha256, read_json, write_json


OPENPARF_ATOMIC_NAME_MAP_SCHEMA = "emuflow.openparf-atomic-name-map/v2"
OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA = (
    "emuflow.openparf-atomic-name-map-storage/v1"
)
OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID = 0x454D554E  # ASCII "EMUN"
OPENPARF_ATOMIC_NAME_MAP_USER_VERSION = 1
_STORE_PREFIX = "name-map."
_STORE_SUFFIX = ".sqlite"
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version",
    "header", "counts", "source", "logical_sha256", "artifact",
    "store_bytes", "store_sha256",
}
_HEADER_REQUIRED = {"schema", "top", "coordinate_system", "site_database"}
_HEADER_OPTIONAL = {"physical_macro_constraints"}
_SOURCE_KEYS = {"mapped_sha256", "packed_sha256", "architecture_sha256"}

_DDL = (Path(__file__).resolve().parents[2] / "data/schema/openparf_name_map.sql").read_text(encoding="utf-8")


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


def _validate_selector_location(path: Path) -> Path:
    if path.is_symlink():
        raise ValidationError("OpenPARF atomic name-map selector is a symbolic link")
    return path.parent.resolve()


class _NameMapLease:
    """Serialize publication/GC against every lazy sidecar reader."""

    def __init__(self, selector_path: Path, *, exclusive: bool) -> None:
        parent = selector_path.parent.resolve()
        lock_path = parent / f".{selector_path.name}.lock"
        if lock_path.is_symlink():
            raise ValidationError("OpenPARF atomic name-map lock is a symbolic link")
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise ValidationError("OpenPARF atomic name-map lock is invalid") from error
        self.closed = False
        try:
            fcntl.flock(
                self.descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except BaseException:
            os.close(self.descriptor)
            self.closed = True
            raise

    def downgrade_shared(self) -> None:
        if self.closed:
            raise ValidationError("OpenPARF atomic name-map lease is closed")
        fcntl.flock(self.descriptor, fcntl.LOCK_SH)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_header(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("OpenPARF atomic name-map header is malformed")
    keys = set(value)
    if (
        not _HEADER_REQUIRED <= keys
        or keys - _HEADER_REQUIRED - _HEADER_OPTIONAL
        or value.get("schema") != OPENPARF_ATOMIC_NAME_MAP_SCHEMA
        or not isinstance(value.get("top"), str)
        or not value["top"]
        or not isinstance(value.get("coordinate_system"), dict)
        or not isinstance(value.get("site_database"), dict)
        or (
            "physical_macro_constraints" in value
            and not isinstance(value["physical_macro_constraints"], dict)
        )
    ):
        raise ValidationError("OpenPARF atomic name-map header is malformed")
    return value


def _validate_selector(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _SELECTOR_KEYS
        or value.get("schema") != OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA
        or value.get("logical_schema") != OPENPARF_ATOMIC_NAME_MAP_SCHEMA
        or isinstance(value.get("application_id"), bool)
        or value.get("application_id") != OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID
        or isinstance(value.get("user_version"), bool)
        or value.get("user_version") != OPENPARF_ATOMIC_NAME_MAP_USER_VERSION
    ):
        raise ValidationError("OpenPARF atomic name-map selector is malformed")
    _validate_header(value.get("header"))
    counts = value.get("counts")
    source = value.get("source")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"atoms", "carry_cascade_chains"}
        or any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in counts.values()
        )
        or counts["atoms"] <= 0
        or not isinstance(source, dict)
        or set(source) != _SOURCE_KEYS
        or not all(_valid_digest(source.get(key)) for key in _SOURCE_KEYS)
        or not _valid_digest(value.get("logical_sha256"))
    ):
        raise ValidationError("OpenPARF atomic name-map selector metadata is malformed")
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
        raise ValidationError("OpenPARF atomic name-map artifact descriptor is invalid")
    return value


def _resolve_store(
    selector_path: Path, selector: Mapping[str, Any]
) -> tuple[Path, StoreSource]:
    selector_parent = _validate_selector_location(selector_path)
    unresolved = selector_path.parent / str(selector["artifact"])
    if unresolved.is_symlink():
        raise ValidationError("OpenPARF atomic name-map store is a symbolic link")
    try:
        store_path = unresolved.resolve(strict=True)
        stat = store_path.stat()
        store_source = bind_store_source(store_path, "openparf-name-map-store-v1")
    except OSError as error:
        raise ValidationError("OpenPARF atomic name-map store is missing") from error
    if store_path.parent != selector_parent:
        raise ValidationError("OpenPARF atomic name-map store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(store_path) != selector["store_sha256"]
    ):
        raise ValidationError("OpenPARF atomic name-map store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return store_path, store_source


def _previous_store(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise ValidationError("previous OpenPARF atomic name map is corrupt") from error
    if (
        isinstance(value, Mapping)
        and value.get("schema") == OPENPARF_ATOMIC_NAME_MAP_SCHEMA
    ):
        return None
    selector = _validate_selector(value)
    return _resolve_store(path, selector)[0]


def _collect_stale_stores(path: Path, keep: Path) -> None:
    """Best-effort GC under the writer lease; failures defer to a later run."""

    changed = False
    for candidate in path.parent.glob(f"{_STORE_PREFIX}*{_STORE_SUFFIX}"):
        if candidate.is_symlink() or candidate.resolve() == keep.resolve():
            continue
        try:
            candidate.unlink()
            changed = True
        except OSError:
            continue
    if changed:
        _fsync_directory(path.parent)


_CASCADE_KEYS = {"id", "cell_type", "instances", "links", "placement"}
_CASCADE_LINK_KEYS = {"source", "target", "signals"}


def _validate_carry_cascade(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CASCADE_KEYS:
        raise ValidationError("OpenPARF atomic name-map cascade is invalid")
    record = dict(value)
    instances = record.get("instances")
    links = record.get("links")
    if (
        not isinstance(record.get("id"), str)
        or not record["id"]
        or record.get("cell_type") != "CARRY8"
        or record.get("placement") != "dedicated-adjacent-chain"
        or not isinstance(instances, list)
        or len(instances) < 2
        or any(not isinstance(item, str) or not item for item in instances)
        or len(set(instances)) != len(instances)
        or not isinstance(links, list)
        or len(links) != len(instances) - 1
    ):
        raise ValidationError("OpenPARF atomic name-map cascade is invalid")
    for index, link in enumerate(links):
        signals = link.get("signals") if isinstance(link, Mapping) else None
        if (
            not isinstance(link, Mapping)
            or set(link) != _CASCADE_LINK_KEYS
            or link.get("source") != instances[index]
            or link.get("target") != instances[index + 1]
            or not isinstance(signals, list)
            or not signals
            or any(not isinstance(signal, str) or not signal for signal in signals)
            or len(set(signals)) != len(signals)
        ):
            raise ValidationError("OpenPARF atomic name-map cascade is invalid")
    return record


def _atom_record(row: sqlite3.Row) -> dict[str, str]:
    return {
        "openparf": str(row["openparf"]),
        "instance": str(row["instance"]),
        "cell_type": str(row["cell_type"]),
        "source_cluster": str(row["source_cluster"]),
        "resource": str(row["resource"]),
    }


class _AtomSequence(Sequence[Mapping[str, str]]):
    def __init__(self, store_path: Path, count: int, owner: Any) -> None:
        self.store_path = store_path
        self.count = count
        self._owner = weakref.ref(owner)

    def _ensure_open(self) -> None:
        owner = self._owner()
        if owner is None:
            raise ValidationError("OpenPARF atomic name-map view is closed")
        owner._ensure_open()

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Mapping[str, str]]:
        self._ensure_open()
        with closing(_connect(self.store_path)) as connection, connection:
            for row in connection.execute(
                "SELECT openparf,instance,cell_type,source_cluster,resource "
                "FROM atoms ORDER BY seq"
            ):
                yield _atom_record(row)

    def __getitem__(self, index: int | slice) -> Any:
        self._ensure_open()
        if isinstance(index, slice):
            return list(iter(self))[index]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        with closing(_connect(self.store_path)) as connection, connection:
            row = connection.execute(
                "SELECT openparf,instance,cell_type,source_cluster,resource "
                "FROM atoms WHERE seq=?", (index,),
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return _atom_record(row)


class _CascadeSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, store_path: Path, count: int, owner: Any) -> None:
        self.store_path = store_path
        self.count = count
        self._owner = weakref.ref(owner)

    def _ensure_open(self) -> None:
        owner = self._owner()
        if owner is None:
            raise ValidationError("OpenPARF atomic name-map view is closed")
        owner._ensure_open()

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        with closing(_connect(self.store_path)) as connection, connection:
            for row in connection.execute(
                "SELECT record_json FROM carry_cascades ORDER BY seq"
            ):
                yield json.loads(str(row[0]))

    def __getitem__(self, index: int | slice) -> Any:
        self._ensure_open()
        if isinstance(index, slice):
            return list(iter(self))[index]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        with closing(_connect(self.store_path)) as connection, connection:
            row = connection.execute(
                "SELECT record_json FROM carry_cascades WHERE seq=?", (index,),
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return json.loads(str(row[0]))


class XilinxOpenparfAtomicNameMapView(Mapping[str, Any]):
    """Checked logical v2 view over one immutable typed sidecar."""

    def __init__(
        self,
        selector_path: Path,
        *,
        mapped_sha256: Optional[str] = None,
        packed_sha256: Optional[str] = None,
        architecture_sha256: Optional[str] = None,
        _lease: Optional[_NameMapLease] = None,
    ) -> None:
        _validate_selector_location(selector_path)
        self._lease = _lease or _NameMapLease(selector_path, exclusive=False)
        self._closed = False
        try:
            self.selector_path = selector_path.resolve()
            selector_source = bind_store_source(
                self.selector_path, "openparf-name-map-selector-v1"
            )
            selector = _validate_selector(read_json(selector_path))
            for key, expected in (
                ("mapped_sha256", mapped_sha256),
                ("packed_sha256", packed_sha256),
                ("architecture_sha256", architecture_sha256),
            ):
                if expected is not None and selector["source"][key] != expected:
                    raise ValidationError(
                        f"OpenPARF atomic name-map {key[:-7]} source seal disagrees"
                    )
            self.selector = selector
            self.store_path, store_source = _resolve_store(selector_path, selector)
            self._source_bindings = (selector_source, store_source)
            self._validate_store()
            require_store_sources_unchanged(self._source_bindings)
            self._atoms = _AtomSequence(
                self.store_path, selector["counts"]["atoms"], self
            )
            self._cascades = _CascadeSequence(
                self.store_path, selector["counts"]["carry_cascade_chains"], self
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_validated_publication(
        cls,
        selector_path: Path,
        selector: dict[str, Any],
        store_path: Path,
        lease: _NameMapLease,
    ) -> "XilinxOpenparfAtomicNameMapView":
        """Reuse writer-owned validation without rereading every atom row."""

        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        value._source_bindings = (
            bind_store_source(value.selector_path, "openparf-name-map-selector-v1"),
            bind_store_source(value.store_path, "openparf-name-map-store-v1"),
        )
        value._atoms = _AtomSequence(
            value.store_path, selector["counts"]["atoms"], value
        )
        value._cascades = _CascadeSequence(
            value.store_path, selector["counts"]["carry_cascade_chains"], value
        )
        return value

    def bound_store_sources(self):
        self._ensure_open()
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("OpenPARF atomic name-map view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "XilinxOpenparfAtomicNameMapView":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_store(self) -> None:
        selector = self.selector
        atom_digest = hashlib.sha256()
        cascade_digest = hashlib.sha256()
        try:
            with closing(_connect(self.store_path)) as connection, connection:
                application_id = int(connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0])
                user_version = int(connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0])
                if (
                    application_id != OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID
                    or user_version != OPENPARF_ATOMIC_NAME_MAP_USER_VERSION
                ):
                    raise ValidationError("unsupported OpenPARF atomic name-map store")
                metadata = {
                    str(row[0]): json.loads(str(row[1]))
                    for row in connection.execute(
                        "SELECT key,value FROM metadata ORDER BY key"
                    )
                }
                expected_metadata = {
                    key: selector[key]
                    for key in ("header", "counts", "source", "logical_sha256")
                }
                if metadata != expected_metadata:
                    raise ValidationError("OpenPARF atomic name-map metadata disagrees")
                atom_count = 0
                for row in connection.execute(
                    "SELECT seq,openparf,instance,cell_type,source_cluster,resource "
                    "FROM atoms ORDER BY seq"
                ):
                    if (
                        int(row["seq"]) != atom_count
                        or str(row["openparf"]) != f"a{atom_count}"
                    ):
                        raise ValidationError(
                            "OpenPARF atomic name-map atom sequence is invalid"
                        )
                    atom = _atom_record(row)
                    if any(not value for value in atom.values()):
                        raise ValidationError(
                            "OpenPARF atomic name-map atom record is invalid"
                        )
                    atom_digest.update(_canonical(atom).encode("utf-8"))
                    atom_digest.update(b"\n")
                    atom_count += 1
                cascade_count = 0
                cascade_member_count = 0
                for row in connection.execute(
                    "SELECT seq,cascade_id,record_json FROM carry_cascades ORDER BY seq"
                ):
                    record = _validate_carry_cascade(
                        json.loads(str(row["record_json"]))
                    )
                    if (
                        int(row["seq"]) != cascade_count
                        or record.get("id") != str(row["cascade_id"])
                    ):
                        raise ValidationError(
                            "OpenPARF atomic name-map cascade record is invalid"
                        )
                    members = list(connection.execute(
                        "SELECT m.member_seq,m.instance,a.cell_type "
                        "FROM cascade_members m JOIN atoms a USING(instance) "
                        "WHERE m.cascade_seq=? ORDER BY m.member_seq",
                        (cascade_count,),
                    ))
                    if (
                        [int(member["member_seq"]) for member in members]
                        != list(range(len(record["instances"])))
                        or [str(member["instance"]) for member in members]
                        != record["instances"]
                        or any(str(member["cell_type"]) != "CARRY8" for member in members)
                    ):
                        raise ValidationError(
                            "OpenPARF atomic name-map cascade ownership is invalid"
                        )
                    cascade_member_count += len(members)
                    cascade_digest.update(_canonical(record).encode("utf-8"))
                    cascade_digest.update(b"\n")
                    cascade_count += 1
                stored_member_count = int(connection.execute(
                    "SELECT COUNT(*) FROM cascade_members"
                ).fetchone()[0])
                if stored_member_count != cascade_member_count:
                    raise ValidationError(
                        "OpenPARF atomic name-map cascade ownership is invalid"
                    )
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("OpenPARF atomic name-map store is corrupt") from error
        if {
            "atoms": atom_count, "carry_cascade_chains": cascade_count,
        } != selector["counts"]:
            raise ValidationError("OpenPARF atomic name-map row counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": selector["header"],
            "counts": selector["counts"],
            "source": selector["source"],
            "atom_sha256": atom_digest.hexdigest(),
            "cascade_sha256": cascade_digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        if logical != selector["logical_sha256"]:
            raise ValidationError("OpenPARF atomic name-map logical seal disagrees")

    def __getitem__(self, key: str) -> Any:
        self._ensure_open()
        if key == "atoms":
            return self._atoms
        if key == "carry_cascade_chains":
            if len(self._cascades):
                return self._cascades
            raise KeyError(key)
        if key in self.selector["header"]:
            return self.selector["header"][key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        yield from self.selector["header"]
        yield "atoms"
        if len(self._cascades):
            yield "carry_cascade_chains"

    def __len__(self) -> int:
        self._ensure_open()
        return len(self.selector["header"]) + 1 + int(bool(len(self._cascades)))

    def iter_atoms(self) -> Iterator[Mapping[str, str]]:
        yield from self._atoms

    def atom_by_ordinal(self, ordinal: int) -> Mapping[str, str]:
        return self._atoms[ordinal]

    def materialize(self) -> dict[str, Any]:
        self._ensure_open()
        return {
            **self.selector["header"],
            "atoms": list(self._atoms),
            **({
                "carry_cascade_chains": list(self._cascades),
            } if len(self._cascades) else {}),
        }




def read_xilinx_openparf_atomic_name_map(
    path: Path,
    *,
    mapped_sha256: Optional[str] = None,
    packed_sha256: Optional[str] = None,
    architecture_sha256: Optional[str] = None,
) -> XilinxOpenparfAtomicNameMapView:
    """Open the current typed owner; whole-map JSON input is retired."""
    return XilinxOpenparfAtomicNameMapView(
        path, mapped_sha256=mapped_sha256, packed_sha256=packed_sha256,
        architecture_sha256=architecture_sha256,
    )
