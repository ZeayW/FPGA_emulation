from __future__ import annotations
from emuflow.physical.openparf.xilinx_openparf_name_map_storage import *
import emuflow.physical.openparf.xilinx_openparf_name_map_storage as _storage
OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID = _storage.OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID
OPENPARF_ATOMIC_NAME_MAP_SCHEMA = _storage.OPENPARF_ATOMIC_NAME_MAP_SCHEMA
OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA = _storage.OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA
OPENPARF_ATOMIC_NAME_MAP_USER_VERSION = _storage.OPENPARF_ATOMIC_NAME_MAP_USER_VERSION
XilinxOpenparfAtomicNameMapView = _storage.XilinxOpenparfAtomicNameMapView
_DDL = _storage._DDL
_NameMapLease = _storage._NameMapLease
_canonical = _storage._canonical
_collect_stale_stores = _storage._collect_stale_stores
_fsync_directory = _storage._fsync_directory
_previous_store = _storage._previous_store
_store_name = _storage._store_name
_valid_digest = _storage._valid_digest
_validate_carry_cascade = _storage._validate_carry_cascade
_validate_header = _storage._validate_header
_validate_selector_location = _storage._validate_selector_location
import hashlib, json, os, sqlite3, tempfile
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable, Iterator, Mapping, Sequence
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.common.native_tools import native_file_generation
from emuflow.storage.store_publication import bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.physical.xilinx_netlist import is_xilinx_mapped_index
from emuflow.physical.xilinx_packing import CONSTANT_TYPES
def write_xilinx_openparf_atomic_name_map_storage(
    path: Path,
    *,
    header: Mapping[str, Any],
    atoms: Iterable[Mapping[str, Any]],
    carry_cascade_chains: Sequence[Mapping[str, Any]],
    mapped_sha256: str,
    packed_sha256: str,
    architecture_sha256: str,
    source_bindings=(),
) -> XilinxOpenparfAtomicNameMapView:
    """Publish a typed atom map and commit it with a bounded selector."""

    normalized_header = _validate_header(dict(header))
    source = {
        "mapped_sha256": mapped_sha256,
        "packed_sha256": packed_sha256,
        "architecture_sha256": architecture_sha256,
    }
    if not all(_valid_digest(value) for value in source.values()):
        raise ValidationError("OpenPARF atomic name-map source seal is invalid")
    source_bindings = validate_store_destination(path, source_bindings)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_selector_location(path)
    lease: Optional[_NameMapLease] = _NameMapLease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    connection: Optional[sqlite3.Connection] = None
    published_store: Optional[Path] = None
    created_store = False
    selector_committed = False
    try:
        _previous_store(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".sqlite.tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        selector_descriptor, selector_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".selector.tmp", dir=path.parent
        )
        os.close(selector_descriptor)
        selector_temporary = Path(selector_name)
        connection = sqlite3.connect(temporary)
        # This database is private until close + explicit fsync + atomic
        # publication.  SQLite's rollback journal would only duplicate writes
        # inside disposable staging and does not strengthen the commit point.
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA application_id={OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID}"
        )
        connection.execute(
            f"PRAGMA user_version={OPENPARF_ATOMIC_NAME_MAP_USER_VERSION}"
        )
        connection.executescript(_DDL)
        atom_digest = hashlib.sha256()
        atom_batch: list[tuple[Any, ...]] = []
        atom_count = 0
        for seq, raw in enumerate(atoms):
            if seq >= 4096:
                raise ValidationError("name-map fixture exceeds record bound")
            if not isinstance(raw, Mapping):
                raise ValidationError("OpenPARF atomic name-map atom is invalid")
            atom = {
                key: raw.get(key)
                for key in (
                    "openparf", "instance", "cell_type", "source_cluster", "resource"
                )
            }
            if (
                atom["openparf"] != f"a{seq}"
                or any(not isinstance(value, str) or not value for value in atom.values())
            ):
                raise ValidationError("OpenPARF atomic name-map atom is invalid")
            atom_batch.append(
                (
                    seq, atom["openparf"], atom["instance"], atom["cell_type"],
                    atom["source_cluster"], atom["resource"],
                )
            )
            if len(atom_batch) >= 8192:
                connection.executemany(
                    "INSERT INTO atoms VALUES (?,?,?,?,?,?)", atom_batch
                )
                atom_batch.clear()
            atom_digest.update(_canonical(atom).encode("utf-8"))
            atom_digest.update(b"\n")
            atom_count += 1
        if atom_batch:
            connection.executemany(
                "INSERT INTO atoms VALUES (?,?,?,?,?,?)", atom_batch
            )
        if atom_count <= 0:
            raise ValidationError("OpenPARF atomic name-map has no atoms")
        cascade_digest = hashlib.sha256()
        for seq, raw in enumerate(carry_cascade_chains):
            record = _validate_carry_cascade(raw)
            encoded = _canonical(record)
            connection.execute(
                "INSERT INTO carry_cascades VALUES (?,?,?)",
                (seq, record["id"], encoded),
            )
            for member_seq, instance in enumerate(record["instances"]):
                row = connection.execute(
                    "SELECT cell_type FROM atoms WHERE instance=?", (instance,)
                ).fetchone()
                if row is None or str(row[0]) != "CARRY8":
                    raise ValidationError(
                        "OpenPARF atomic name-map cascade ownership is invalid"
                    )
                connection.execute(
                    "INSERT INTO cascade_members VALUES (?,?,?)",
                    (seq, member_seq, instance),
                )
            cascade_digest.update(encoded.encode("utf-8"))
            cascade_digest.update(b"\n")
        counts = {
            "atoms": atom_count,
            "carry_cascade_chains": len(carry_cascade_chains),
        }
        logical_sha = hashlib.sha256(_canonical({
            "header": normalized_header,
            "counts": counts,
            "source": source,
            "atom_sha256": atom_digest.hexdigest(),
            "cascade_sha256": cascade_digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        for key, value in (
            ("header", normalized_header), ("counts", counts),
            ("source", source), ("logical_sha256", logical_sha),
        ):
            connection.execute(
                "INSERT INTO metadata VALUES (?,?)", (key, _canonical(value))
            )
        connection.commit()
        connection.close()
        connection = None
        store_sha = file_sha256(temporary)
        published_store = path.with_name(_store_name(store_sha))
        if published_store.is_symlink():
            raise ValidationError(
                "existing OpenPARF atomic name-map store is a symbolic link"
            )
        require_store_sources_unchanged(source_bindings)
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError(
                    "existing OpenPARF atomic name-map artifact seal disagrees"
                )
            temporary.unlink()
        else:
            publish_sqlite_store(
                temporary,
                published_store,
                "openparf-name-map-store-v1",
                sources=source_bindings,
            )
            created_store = True
        selector = {
            "schema": OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA,
            "logical_schema": OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
            "application_id": OPENPARF_ATOMIC_NAME_MAP_APPLICATION_ID,
            "user_version": OPENPARF_ATOMIC_NAME_MAP_USER_VERSION,
            "header": normalized_header,
            "counts": counts,
            "source": source,
            "logical_sha256": logical_sha,
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        }
        # The writer has already checked every logical record, enforced all
        # unique keys in SQLite and computed the content seal.  Do not reopen
        # and rescan the graph solely to manufacture the returned view.
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        require_store_sources_unchanged(source_bindings)
        os.replace(selector_temporary, path)
        selector_committed = True
        _fsync_directory(path.parent)
        # GC is post-commit housekeeping.  Failure cannot turn a visible valid
        # selector into a reported publication failure; the next exclusive
        # writer retries every stale immutable sidecar.
        try:
            _collect_stale_stores(path, published_store)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = XilinxOpenparfAtomicNameMapView._from_validated_publication(
            path, selector, published_store, lease
        )
        lease = None
        return view
    except BaseException as error:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if selector_temporary is not None:
            selector_temporary.unlink(missing_ok=True)
        if published_store is not None and created_store and not selector_committed:
            published_store.unlink(missing_ok=True)
        if isinstance(error, sqlite3.IntegrityError):
            raise ValidationError(
                "OpenPARF atomic name-map identity is duplicated"
            ) from error
        raise
    finally:
        if lease is not None:
            lease.close()
