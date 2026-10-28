"""Independent placement/certificate oracle for explicit differential tests.

This module has no production dispatch, solver launcher or fallback route.
"""
from __future__ import annotations

from contextlib import closing

from collections import Counter, defaultdict

from collections.abc import Mapping as MappingABC

import fcntl

import errno

import hashlib

import json

import math

import os

from pathlib import Path

import re

import sqlite3

import subprocess

import tempfile

from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri

from emuflow.platform.architecture import ArchitectureDB

from emuflow.common.errors import ImportError, ValidationError

from emuflow.storage.store_publication import bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination

from emuflow.common.io import file_sha256, read_json, write_json


from emuflow.physical.xilinx_native_device_constraints import load_xilinx_native_device_constraints, require_xilinx_native_constraint_capability

from emuflow.physical.xilinx_packing import DUAL_OUTPUT_LUT_TYPE

from emuflow.physical.xilinx_packing import FF_TYPES

from emuflow.physical.xilinx_packing import LUT_TYPES

from emuflow.physical.xilinx_physical_macros import build_xilinx_physical_macro_contract

from emuflow.physical.openparf.xilinx_openparf_source_storage import OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA, materialize_xilinx_openparf_atomic_source, read_xilinx_openparf_atomic_source

from emuflow.physical.openparf.xilinx_openparf_name_map_storage import OPENPARF_ATOMIC_NAME_MAP_SCHEMA, OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA, XilinxOpenparfAtomicNameMapView, read_xilinx_openparf_atomic_name_map

OPENPARF_ATOMIC_MANIFEST_SCHEMA = "emuflow.openparf-atomic-manifest/v1"

OPENPARF_ATOMIC_PLACEMENT_SCHEMA = "emuflow.openparf-atomic-placement/v2"

OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA = (
    "emuflow.openparf-atomic-placement-database/v3"
)

_LEGACY_OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA = (
    "emuflow.openparf-atomic-placement-database/v2"
)

OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID = 0x454D5550  # "EMUP"

OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION = 3

_PLACEMENT_CERTIFICATE_STORE_SUFFIX = ".sqlite"

OPENPARF_ATOMIC_SOURCE_SCHEMA = "emuflow.openparf-atomic-source/v1"

OPENPARF_ATOMIC_PROVIDER = (
    "openparf-native-rudy-pin-aware-mcf-direct-lg-ism-atomic-v8"
)

XILINX_OPENPARF_SITE_DATABASE_SCHEMA = (
    "emuflow.openparf-atomic-site-database/v1"
)

OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA = (
    "openparf.physical-macro-groups/v3"
)

_HARD_RESOURCES = {
    "DSP48E2": "DSP48E2",
    "RAMB18E2": "RAMB18E2",
    "RAMB36E2": "RAMB36E2",
    "URAM288": "URAM288",
}

_MUX_RESOURCES = {
    "MUXF7": "MUXF7",
    "MUXF8": "MUXF8",
    "MUXF9": "MUXF9",
}

_MUX_BELS = {
    "MUXF7": ("F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH"),
    "MUXF8": ("F8MUX_BOT", "F8MUX_TOP"),
    "MUXF9": ("F9MUX",),
}

_SLICE_LUT_TYPES = LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE}

_FF_CLOCK = "C"

_FF_ENABLE = "CE"

_FF_SR = {"FDCE": "CLR", "FDRE": "R", "FDPE": "PRE", "FDSE": "S"}

OPENPARF_FF_CONTROL_LEGALIZATION_SCHEMA = (
    "emuflow.openparf-ff-control-legalization/v1"
)

OPENPARF_RESULT_CHECKPOINT_SCHEMA = "emuflow.openparf-result-checkpoint/v1"

class _IndexedAtomicPlacement:
    """Disk-backed join of native ``.pl`` rows to the typed atom contract.

    The name-map reader has already authenticated every atom row and owns a
    shared lease on its immutable sidecar.  Stage only the solver coordinates
    here, then let SQLite prove one-to-one coverage and stream the joined rows
    in authoritative atom order.  This avoids retaining both a graph-sized
    ``openparf -> atom`` dictionary and a second ``openparf -> coordinate``
    dictionary beside the final placement certificate.
    """

    def __init__(
        self,
        placement_path: Path,
        name_map: XilinxOpenparfAtomicNameMapView,
    ) -> None:
        self._name_map: Optional[XilinxOpenparfAtomicNameMapView] = name_map
        self._path: Optional[Path] = None
        self._database: Optional[sqlite3.Connection] = None
        descriptor, temporary = tempfile.mkstemp(
            prefix="emuflow-openparf-placement-", suffix=".sqlite3",
        )
        os.close(descriptor)
        self._path = Path(temporary)
        try:
            # The authenticated atom sidecar is attached by a read-only file
            # URI below. URI parsing must be enabled on the staging connection
            # as well; some production SQLite builds disable it by default.
            database = sqlite3.connect(self._path, uri=True)
            self._database = database
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA synchronous=OFF")
            database.execute("PRAGMA temp_store=FILE")
            database.execute("PRAGMA cache_size=-4096")
            database.execute("""
                CREATE TABLE placement(
                    openparf TEXT PRIMARY KEY,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    z INTEGER NOT NULL,
                    source_line INTEGER NOT NULL
                ) WITHOUT ROWID
            """)
            database.execute("""
                CREATE TABLE occupancy(
                    site TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    z INTEGER NOT NULL,
                    PRIMARY KEY(site,resource,z)
                ) WITHOUT ROWID
            """)
            database.execute("""
                CREATE TABLE normalized(
                    seq INTEGER PRIMARY KEY,
                    instance TEXT NOT NULL UNIQUE,
                    cell_type TEXT NOT NULL,
                    source_cluster TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    z INTEGER NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    site TEXT NOT NULL,
                    anchor_site TEXT NOT NULL,
                    bel TEXT NOT NULL,
                    placement_mode TEXT NOT NULL,
                    bram_tile_group TEXT
                )
            """)
            database.execute("""
                CREATE TABLE requested_instances(
                    seq INTEGER PRIMARY KEY,
                    instance TEXT NOT NULL
                )
            """)
            batch: list[tuple[str, int, int, int, int]] = []
            atom_count = int(name_map.selector["counts"]["atoms"])
            max_ordinal_digits = len(str(atom_count - 1))
            seen_ordinals = bytearray((atom_count + 7) // 8)

            def flush_pending() -> None:
                if batch:
                    self._insert(batch)
                    batch.clear()

            with placement_path.open("r", encoding="utf-8") as stream:
                for line_number, raw in enumerate(stream, start=1):
                    fields = raw.strip().split()
                    if not fields or fields[0].startswith("#"):
                        continue
                    if len(fields) not in {4, 5}:
                        flush_pending()
                        raise ImportError(
                            f"{placement_path}:{line_number}: invalid atomic "
                            "placement row"
                        )
                    ordinal = fields[0][1:]
                    valid_ordinal = (
                        fields[0].startswith("a")
                        and ordinal.isascii()
                        and ordinal.isdecimal()
                        and (ordinal == "0" or not ordinal.startswith("0"))
                        and len(ordinal) <= max_ordinal_digits
                    )
                    ordinal_value = -1
                    if valid_ordinal:
                        ordinal_value = int(ordinal)
                        valid_ordinal = (
                            fields[0] == f"a{ordinal_value}"
                            and ordinal_value < atom_count
                        )
                    if not valid_ordinal:
                        flush_pending()
                        raise ImportError(
                            f"{placement_path}:{line_number}: invalid atomic "
                            "placement row"
                        )
                    byte_index, bit_index = divmod(ordinal_value, 8)
                    ordinal_bit = 1 << bit_index
                    if seen_ordinals[byte_index] & ordinal_bit:
                        flush_pending()
                        raise ValidationError(
                            "OpenPARF atomic placement duplicates an atom"
                        )
                    try:
                        values = [float(value) for value in fields[1:4]]
                    except ValueError as error:
                        flush_pending()
                        raise ImportError(
                            "OpenPARF atomic placement is non-numeric"
                        ) from error
                    if any(
                        not math.isfinite(value) or not value.is_integer()
                        for value in values
                    ):
                        flush_pending()
                        raise ValidationError(
                            "OpenPARF atomic placement is not discrete"
                        )
                    seen_ordinals[byte_index] |= ordinal_bit
                    batch.append((
                        fields[0], *(int(value) for value in values), line_number,
                    ))
                    if len(batch) >= 8192:
                        flush_pending()
                flush_pending()
            database.commit()
            atom_uri = name_map.store_path.resolve().as_uri() + (
                "?mode=ro&immutable=1"
            )
            database.execute("ATTACH DATABASE ? AS atom_map", (atom_uri,))
            unknown = database.execute(
                "SELECT p.source_line FROM placement p "
                "LEFT JOIN atom_map.atoms a USING(openparf) "
                "WHERE a.seq IS NULL ORDER BY p.source_line LIMIT 1"
            ).fetchone()
            if unknown is not None:
                raise ImportError(
                    f"{placement_path}:{int(unknown[0])}: invalid atomic "
                    "placement row"
                )
            placed_count = int(database.execute(
                "SELECT COUNT(*) FROM placement"
            ).fetchone()[0])
            if placed_count != atom_count or database.execute(
                "SELECT 1 FROM atom_map.atoms a "
                "LEFT JOIN placement p USING(openparf) "
                "WHERE p.openparf IS NULL LIMIT 1"
            ).fetchone() is not None:
                raise ValidationError(
                    "OpenPARF atomic placement does not cover every atom"
                )
        except BaseException:
            self.close()
            raise

    def _insert(self, rows: Sequence[tuple[str, int, int, int, int]]) -> None:
        assert self._database is not None
        try:
            self._database.executemany(
                "INSERT INTO placement VALUES (?, ?, ?, ?, ?)", rows
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "OpenPARF atomic placement duplicates an atom"
            ) from error

    def requested_coordinates(self) -> list[tuple[int, int]]:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        return [
            (int(row[0]), int(row[1]))
            for row in self._database.execute(
                "SELECT DISTINCT x,y FROM placement ORDER BY x,y"
            )
        ]

    def iter_joined(
        self,
    ) -> Iterator[tuple[Mapping[str, str], tuple[int, int, int]]]:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        rows = self._database.execute(
            "SELECT a.openparf,a.instance,a.cell_type,a.source_cluster,a.resource,"
            "p.x,p.y,p.z FROM atom_map.atoms a JOIN placement p USING(openparf) "
            "ORDER BY a.seq"
        )
        for row in rows:
            yield ({
                "openparf": str(row[0]), "instance": str(row[1]),
                "cell_type": str(row[2]), "source_cluster": str(row[3]),
                "resource": str(row[4]),
            }, (int(row[5]), int(row[6]), int(row[7])))

    def claim_occupancy(self, site: str, resource: str, z: int) -> None:
        """Record one exact physical slot without a design-sized Python set."""

        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        try:
            self._database.execute(
                "INSERT INTO occupancy VALUES (?, ?, ?)", (site, resource, z)
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "OpenPARF atomic placement overlaps a BEL slot"
            ) from error

    def append_normalized(
        self,
        atom: Mapping[str, str],
        *,
        z: int,
        x: float,
        y: float,
        site: str,
        anchor_site: str,
        bel: str,
        placement_mode: str,
        bram_tile_group: Optional[Mapping[str, Any]],
    ) -> None:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        encoded_group = (
            json.dumps(bram_tile_group, sort_keys=True, separators=(",", ":"))
            if bram_tile_group is not None else None
        )
        try:
            self._database.execute(
                "INSERT INTO normalized VALUES "
                "((SELECT seq FROM atom_map.atoms WHERE openparf=?),"
                "?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    atom["openparf"], atom["instance"], atom["cell_type"],
                    atom["source_cluster"], atom["resource"], z, x, y, site,
                    anchor_site, bel, placement_mode, encoded_group,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "OpenPARF normalized placement identity is duplicated"
            ) from error

    @staticmethod
    def _normalized_record(row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "instance": str(row[0]), "cell_type": str(row[1]),
            "source_cluster": str(row[2]), "resource": str(row[3]),
            "z": int(row[4]), "x": float(row[5]), "y": float(row[6]),
            "site": str(row[7]), "anchor_site": str(row[8]),
            "bel": str(row[9]), "placement_mode": str(row[10]),
            "bram_tile_group": (
                json.loads(str(row[11])) if row[11] is not None else None
            ),
        }

    def instances(self, names: Sequence[str]) -> List[Dict[str, Any]]:
        """Resolve one bounded macro/cascade membership in caller order."""

        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        self._database.execute("DELETE FROM requested_instances")
        self._database.executemany(
            "INSERT INTO requested_instances VALUES (?, ?)", enumerate(names)
        )
        missing = self._database.execute(
            "SELECT r.instance FROM requested_instances r "
            "LEFT JOIN normalized n USING(instance) "
            "WHERE n.seq IS NULL ORDER BY r.seq LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise KeyError(str(missing[0]))
        return [
            self._normalized_record(row)
            for row in self._database.execute(
                "SELECT n.instance,n.cell_type,n.source_cluster,n.resource,n.z,"
                "n.x,n.y,n.site,n.anchor_site,n.bel,n.placement_mode,"
                "n.bram_tile_group FROM requested_instances r "
                "JOIN normalized n USING(instance) ORDER BY r.seq"
            )
        ]

    def iter_site_groups(
        self,
    ) -> Iterator[tuple[str, List[Dict[str, Any]]]]:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        rows = self._database.execute(
            "SELECT instance,cell_type,source_cluster,resource,z,x,y,site,"
            "anchor_site,bel,placement_mode,bram_tile_group FROM normalized "
            "ORDER BY anchor_site,instance"
        )
        current_site: Optional[str] = None
        current: List[Dict[str, Any]] = []
        for row in rows:
            site = str(row[8])
            if current_site is not None and site != current_site:
                yield current_site, current
                current = []
            current_site = site
            current.append(self._normalized_record(row))
        if current_site is not None:
            yield current_site, current

    def hard_resource_instances(self) -> set[str]:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        placeholders = ",".join("?" for _ in _HARD_RESOURCES)
        return {
            str(row[0]) for row in self._database.execute(
                f"SELECT instance FROM normalized WHERE resource IN ({placeholders})",
                tuple(_HARD_RESOURCES.values()),
            )
        }

    def summary(self) -> tuple[int, Counter[str]]:
        if self._database is None:
            raise ValidationError("OpenPARF atomic placement index is closed")
        total = int(self._database.execute(
            "SELECT COUNT(*) FROM normalized"
        ).fetchone()[0])
        counts: Counter[str] = Counter({
            str(row[0]): int(row[1])
            for row in self._database.execute(
                "SELECT resource,COUNT(*) FROM normalized GROUP BY resource"
            )
        })
        return total, counts

    def close(self) -> None:
        database = self._database
        self._database = None
        if database is not None:
            database.close()
        path = self._path
        self._path = None
        if path is not None:
            path.unlink(missing_ok=True)
        name_map = self._name_map
        self._name_map = None
        if name_map is not None:
            name_map.close()

    def __del__(self) -> None:
        if hasattr(self, "closed"):
            self.close()

    def __enter__(self) -> "_IndexedAtomicPlacement":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

def _site_claim(site_name: str) -> List[str]:
    return [f"site:{site_name}"]

def _bram_claims(tile: str, role: str) -> List[str]:
    if role == "lower":
        return [f"bram:{tile}:lower"]
    if role == "upper":
        return [f"bram:{tile}:upper"]
    if role == "whole":
        return [f"bram:{tile}:lower", f"bram:{tile}:upper"]
    raise ValidationError(f"unknown BRAM tile role {role!r}")

def _physical_input_bindings(paths, *views):
    """Keep raw input generations and borrow registered indexed owner scopes."""
    sources = tuple(
        bind_store_source(path, "physical-input-document-v1")
        for path in paths
        if path is not None
    )
    for view in views:
        bind = getattr(view, "bound_store_sources", None)
        if callable(bind):
            sources += bind()
    require_store_sources_unchanged(sources)
    return sources

def _sha256(path: Path) -> str:
    return file_sha256(path)

class _PlacementCertificateLease:
    """Keep a published certificate generation alive for lazy readers."""

    def __init__(self, selector_path: Path, *, exclusive: bool) -> None:
        parent = selector_path.parent.resolve()
        lock_path = parent / f".{selector_path.name}.lock"
        if lock_path.is_symlink():
            raise ValidationError(
                "OpenPARF placement-certificate lock is a symbolic link"
            )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise ValidationError(
                "OpenPARF placement-certificate lock is invalid"
            ) from error
        try:
            fcntl.flock(
                self._descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except BaseException:
            os.close(self._descriptor)
            raise
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)

def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _certificate_store_name(path: Path, digest: str) -> str:
    return f"{path.stem}.{digest}{_PLACEMENT_CERTIFICATE_STORE_SUFFIX}"

_PLACEMENT_TABLE_XINFO = {
    "metadata": [
        (0, "key", "TEXT", 1, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
    ],
    "clusters": [
        (0, "cluster_index", "INTEGER", 0, None, 1, 0),
        (1, "cluster", "TEXT", 1, None, 0, 0),
        (2, "site", "TEXT", 1, None, 0, 0),
        (3, "site_type", "TEXT", 1, None, 0, 0),
        (4, "x", "INTEGER", 1, None, 0, 0),
        (5, "y", "INTEGER", 1, None, 0, 0),
    ],
    "assignments": [
        (0, "cluster_index", "INTEGER", 1, None, 1, 0),
        (1, "assignment_index", "INTEGER", 1, None, 2, 0),
        (2, "instance", "TEXT", 1, None, 0, 0),
        (3, "cell_type", "TEXT", 1, None, 0, 0),
        (4, "bel", "TEXT", 1, None, 0, 0),
        (5, "physical_site", "TEXT", 1, None, 0, 0),
        (6, "placement_mode", "TEXT", 1, None, 0, 0),
        (7, "source_cluster", "TEXT", 1, None, 0, 0),
        (8, "bram_tile_group", "TEXT", 0, None, 0, 0),
    ],
}

def _logical_record_digest(
    header: Mapping[str, Any], database: sqlite3.Connection,
) -> str:
    """Hash logical records independently of SQLite page/index layout."""

    digest = hashlib.sha256()

    def append(kind: str, value: Any) -> None:
        encoded = json.dumps(
            {"kind": kind, "value": value}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    append("header/v1", dict(header))
    for row in database.execute(
        "SELECT cluster_index,cluster,site,site_type,x,y FROM clusters "
        "ORDER BY cluster_index"
    ):
        append("cluster/v1", list(row))
    for row in database.execute(
        "SELECT cluster_index,assignment_index,instance,cell_type,bel,"
        "physical_site,placement_mode,source_cluster,bram_tile_group "
        "FROM assignments ORDER BY cluster_index,assignment_index"
    ):
        append("assignment/v1", list(row))
    return digest.hexdigest()

def _validate_placement_database_contract(
    database: sqlite3.Connection,
    *,
    descriptor: Mapping[str, Any],
    header: Mapping[str, Any],
    legacy: bool,
) -> None:
    if not legacy and (
        int(database.execute("PRAGMA application_id").fetchone()[0])
        != OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID
        or int(database.execute("PRAGMA user_version").fetchone()[0])
        != OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION
    ):
        raise ValidationError("OpenPARF atomic placement database identity is invalid")
    objects = list(database.execute(
        "SELECT type,name,tbl_name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    if objects != [
        ("table", "assignments", "assignments"),
        ("table", "clusters", "clusters"),
        ("table", "metadata", "metadata"),
    ]:
        raise ValidationError("OpenPARF atomic placement database schema is invalid")
    for table, expected in _PLACEMENT_TABLE_XINFO.items():
        actual = [tuple(row) for row in database.execute(
            f"PRAGMA table_xinfo({table})"
        )]
        if actual != expected:
            raise ValidationError(
                "OpenPARF atomic placement database column contract is invalid"
            )
    metadata = dict(database.execute("SELECT key,value FROM metadata"))
    expected_metadata = {
        "schema": (
            _LEGACY_OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA
            if legacy else OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA
        ),
        "clusters": str(descriptor["clusters"]),
        "assignments": str(descriptor["assignments"]),
    }
    if not legacy:
        expected_metadata["logical_sha256"] = str(descriptor["logical_sha256"])
    if metadata != expected_metadata:
        raise ValidationError("OpenPARF atomic placement database metadata is invalid")
    cluster_count = int(database.execute("SELECT COUNT(*) FROM clusters").fetchone()[0])
    assignment_count = int(database.execute(
        "SELECT COUNT(*) FROM assignments"
    ).fetchone()[0])
    if (
        cluster_count != descriptor["clusters"]
        or assignment_count != descriptor["assignments"]
        or database.execute(
            "SELECT 1 FROM assignments a LEFT JOIN clusters c "
            "USING(cluster_index) WHERE c.cluster_index IS NULL LIMIT 1"
        ).fetchone() is not None
        or database.execute(
            "SELECT 1 FROM clusters c LEFT JOIN assignments a "
            "USING(cluster_index) WHERE a.cluster_index IS NULL LIMIT 1"
        ).fetchone() is not None
        or database.execute(
            "SELECT 1 FROM (SELECT MIN(cluster_index) lo,MAX(cluster_index) hi,"
            "COUNT(*) n FROM clusters) WHERE lo!=0 OR hi+1!=n LIMIT 1"
        ).fetchone() is not None
        or database.execute(
            "SELECT 1 FROM assignments GROUP BY cluster_index "
            "HAVING MIN(assignment_index)!=0 OR "
            "MAX(assignment_index)+1!=COUNT(*) LIMIT 1"
        ).fetchone() is not None
    ):
        raise ValidationError("OpenPARF atomic placement database coverage is invalid")
    if not legacy and _logical_record_digest(header, database) != descriptor[
        "logical_sha256"
    ]:
        raise ValidationError("OpenPARF atomic placement logical digest disagrees")

def _validate_atomic_placement_descriptor(
    path: Path,
    descriptor: Any,
    *,
    source_bindings=None,
) -> Path:
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != {
            "schema", "file", "bytes", "sha256", "clusters", "assignments",
            "application_id", "user_version", "logical_sha256",
        }
        or descriptor.get("schema")
        != OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA
        or descriptor.get("application_id")
        != OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID
        or descriptor.get("user_version")
        != OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION
        or not isinstance(descriptor.get("file"), str)
        or Path(descriptor["file"]).name != descriptor["file"]
        or not isinstance(descriptor.get("clusters"), int)
        or isinstance(descriptor.get("clusters"), bool)
        or not isinstance(descriptor.get("assignments"), int)
        or isinstance(descriptor.get("assignments"), bool)
        or descriptor["clusters"] <= 0
        or descriptor["assignments"] <= 0
        or not isinstance(descriptor.get("bytes"), int)
        or isinstance(descriptor.get("bytes"), bool)
        or descriptor["bytes"] <= 0
        or not isinstance(descriptor.get("sha256"), str)
        or len(descriptor["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in descriptor["sha256"])
        or not isinstance(descriptor.get("logical_sha256"), str)
        or len(descriptor["logical_sha256"]) != 64
        or any(
            c not in "0123456789abcdef"
            for c in descriptor["logical_sha256"]
        )
        or descriptor["file"]
        != _certificate_store_name(path, descriptor["sha256"])
    ):
        raise ValidationError(
            "OpenPARF atomic placement storage descriptor is invalid"
        )
    if path.is_symlink():
        raise ValidationError(
            "OpenPARF atomic placement certificate is a symbolic link"
        )
    unresolved = path.parent / descriptor["file"]
    if unresolved.is_symlink():
        raise ValidationError(
            "OpenPARF atomic placement database is a symbolic link"
        )
    try:
        database_path = unresolved.resolve(strict=True)
        stat = database_path.stat()
        source = bind_store_source(database_path, "openparf-placement-certificate-v3")
    except OSError as error:
        raise ValidationError(
            "OpenPARF atomic placement database is missing"
        ) from error
    if database_path.parent != path.parent.resolve():
        raise ValidationError(
            "OpenPARF atomic placement database escapes its selector"
        )
    if (
        stat.st_size != descriptor["bytes"]
        or _sha256(database_path) != descriptor["sha256"]
    ):
        raise ValidationError("OpenPARF atomic placement database seal is invalid")
    require_store_sources_unchanged((source,))
    if source_bindings is not None:
        source_bindings.append(source)
    return database_path

def _valid_unreferenced_certificate_store(
    selector_path: Path, candidate: Path,
) -> bool:
    """Recognize only a complete writer-owned generation before orphan GC."""

    prefix = f"{selector_path.stem}."
    if candidate.is_symlink() or not candidate.name.endswith(
        _PLACEMENT_CERTIFICATE_STORE_SUFFIX
    ) or not candidate.name.startswith(prefix):
        return False
    digest = candidate.name[
        len(prefix):-len(_PLACEMENT_CERTIFICATE_STORE_SUFFIX)
    ]
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or _sha256(candidate) != digest
    ):
        return False
    try:
        with closing(sqlite3.connect(
            immutable_sqlite_uri(candidate), uri=True
        )) as database, database:
            if (
                int(database.execute("PRAGMA application_id").fetchone()[0])
                != OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID
                or int(database.execute("PRAGMA user_version").fetchone()[0])
                != OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION
                or list(database.execute(
                    "SELECT type,name,tbl_name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )) != [
                    ("table", "assignments", "assignments"),
                    ("table", "clusters", "clusters"),
                    ("table", "metadata", "metadata"),
                ]
            ):
                return False
            metadata = dict(database.execute("SELECT key,value FROM metadata"))
            if (
                set(metadata)
                != {"schema", "clusters", "assignments", "logical_sha256"}
                or metadata["schema"]
                != OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA
                or len(metadata["logical_sha256"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in metadata["logical_sha256"]
                )
                or any(
                    [tuple(row) for row in database.execute(
                        f"PRAGMA table_xinfo({table})"
                    )] != expected
                    for table, expected in _PLACEMENT_TABLE_XINFO.items()
                )
            ):
                return False
            clusters = int(metadata["clusters"])
            assignments = int(metadata["assignments"])
            return (
                clusters > 0
                and assignments > 0
                and database.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
                == clusters
                and database.execute(
                    "SELECT COUNT(*) FROM assignments"
                ).fetchone()[0] == assignments
                and database.execute(
                    "SELECT 1 FROM assignments a LEFT JOIN clusters c "
                    "USING(cluster_index) WHERE c.cluster_index IS NULL LIMIT 1"
                ).fetchone() is None
                and database.execute(
                    "SELECT 1 FROM clusters c LEFT JOIN assignments a "
                    "USING(cluster_index) WHERE a.cluster_index IS NULL LIMIT 1"
                ).fetchone() is None
                and database.execute(
                    "SELECT 1 FROM (SELECT MIN(cluster_index) lo,"
                    "MAX(cluster_index) hi,COUNT(*) n FROM clusters) "
                    "WHERE lo!=0 OR hi+1!=n LIMIT 1"
                ).fetchone() is None
                and database.execute(
                    "SELECT 1 FROM assignments GROUP BY cluster_index "
                    "HAVING MIN(assignment_index)!=0 OR "
                    "MAX(assignment_index)+1!=COUNT(*) LIMIT 1"
                ).fetchone() is None
            )
    except (OSError, ValueError, sqlite3.DatabaseError):
        return False

class _AtomicPlacementCertificateBuilder:
    """Stream validated site groups into one typed certificate generation."""

    def __init__(self, selector_path: Path, *, source_bindings=()) -> None:
        self.closed = True
        self.source_bindings = validate_store_destination(
            selector_path, source_bindings
        )
        selector_path.parent.mkdir(parents=True, exist_ok=True)
        self.selector_path = selector_path
        self.lease = _PlacementCertificateLease(selector_path, exclusive=True)
        self.closed = False
        self.created_store = None
        self.selector_committed = False
        self.temporary_path: Optional[Path] = None
        self.database: Optional[sqlite3.Connection] = None
        self.previous_store: Optional[Path] = None
        self.cluster_count = 0
        self.assignment_count = 0
        try:
            if selector_path.exists():
                if selector_path.is_symlink():
                    raise ValidationError(
                        "previous OpenPARF placement certificate is a symbolic link"
                    )
                try:
                    previous = read_json(selector_path)
                except (OSError, ValueError) as error:
                    raise ValidationError(
                        "previous OpenPARF placement certificate is corrupt"
                    ) from error
                self.previous_store = _validate_atomic_placement_descriptor(
                    selector_path, previous.get("cluster_storage")
                )
                previous_descriptor = previous["cluster_storage"]
                previous_header = dict(previous)
                previous_header.pop("cluster_storage")
                try:
                    with closing(sqlite3.connect(
                        immutable_sqlite_uri(self.previous_store),
                        uri=True,
                    )) as previous_database, previous_database:
                        _validate_placement_database_contract(
                            previous_database,
                            descriptor=previous_descriptor,
                            header=previous_header,
                            legacy=False,
                        )
                except sqlite3.DatabaseError as error:
                    raise ValidationError(
                        "previous OpenPARF placement-certificate store is invalid"
                    ) from error
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{selector_path.name}.", suffix=".sqlite.tmp",
                dir=selector_path.parent,
            )
            os.close(descriptor)
            self.temporary_path = Path(temporary)
            self.database = sqlite3.connect(self.temporary_path)
            self.database.executescript(f"""
            PRAGMA page_size = 65536;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = FULL;
            PRAGMA temp_store = FILE;
            PRAGMA application_id = {OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID};
            PRAGMA user_version = {OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION};
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE clusters (
                cluster_index INTEGER PRIMARY KEY,
                cluster TEXT NOT NULL UNIQUE,
                site TEXT NOT NULL UNIQUE,
                site_type TEXT NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL
            );
            CREATE TABLE assignments (
                cluster_index INTEGER NOT NULL REFERENCES clusters(cluster_index),
                assignment_index INTEGER NOT NULL,
                instance TEXT NOT NULL UNIQUE,
                cell_type TEXT NOT NULL,
                bel TEXT NOT NULL,
                physical_site TEXT NOT NULL,
                placement_mode TEXT NOT NULL,
                source_cluster TEXT NOT NULL,
                bram_tile_group TEXT CHECK(
                    bram_tile_group IS NULL OR json_valid(bram_tile_group)
                ),
                PRIMARY KEY (cluster_index, assignment_index)
            ) WITHOUT ROWID;
            """)
        except sqlite3.DatabaseError as error:
            self.close()
            raise ValidationError(
                "OpenPARF atomic placement database is invalid"
            ) from error
        except BaseException:
            self.close()
            raise

    def append(self, cluster: Mapping[str, Any]) -> None:
        assignments = cluster.get("assignments")
        if (
            not isinstance(assignments, Sequence)
            or isinstance(assignments, (str, bytes))
            or not assignments
        ):
            raise ValidationError("OpenPARF atomic placement cluster is invalid")
        cluster_index = self.cluster_count
        if self.database is None:
            raise ValidationError("OpenPARF placement-certificate writer is closed")
        try:
            self.database.execute(
                "INSERT INTO clusters VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cluster_index, str(cluster["cluster"]), str(cluster["site"]),
                    str(cluster["site_type"]), int(cluster["x"]), int(cluster["y"]),
                ),
            )
            self.database.executemany(
                "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        cluster_index, assignment_index,
                        str(assignment["instance"]),
                        str(assignment["cell_type"]), str(assignment["bel"]),
                        str(assignment["physical_site"]),
                        str(assignment["placement_mode"]),
                        str(assignment["source_cluster"]),
                        (
                            json.dumps(
                                assignment["bram_tile_group"], sort_keys=True,
                                separators=(",", ":"),
                            )
                            if assignment.get("bram_tile_group") is not None
                            else None
                        ),
                    )
                    for assignment_index, assignment in enumerate(assignments)
                ),
            )
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as error:
            raise ValidationError(
                "OpenPARF atomic placement cluster cannot be stored"
            ) from error
        self.cluster_count += 1
        self.assignment_count += len(assignments)

    def publish(self, header: Mapping[str, Any]) -> Dict[str, Any]:
        if self.cluster_count <= 0 or self.assignment_count <= 0:
            raise ValidationError("OpenPARF atomic placement clusters are invalid")
        if self.database is None or self.temporary_path is None:
            raise ValidationError("OpenPARF placement-certificate writer is closed")
        require_store_sources_unchanged(self.source_bindings)
        compact = dict(header)
        compact.pop("clusters", None)
        compact.pop("cluster_storage", None)
        logical_sha256 = _logical_record_digest(compact, self.database)
        self.database.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            (
                ("schema", OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA),
                ("clusters", str(self.cluster_count)),
                ("assignments", str(self.assignment_count)),
                ("logical_sha256", logical_sha256),
            ),
        )
        self.database.commit()
        self.database.close()
        self.database = None
        digest = _sha256(self.temporary_path)
        store_path = self.selector_path.parent / _certificate_store_name(
            self.selector_path, digest
        )
        if store_path.is_symlink():
            raise ValidationError(
                "OpenPARF atomic placement database is a symbolic link"
            )
        require_store_sources_unchanged(self.source_bindings)
        if store_path.exists():
            if _sha256(store_path) != digest:
                raise ValidationError(
                    "OpenPARF atomic placement content-addressed store disagrees"
                )
            self.temporary_path.unlink()
        else:
            publish_sqlite_store(
                self.temporary_path,
                store_path,
                "openparf-placement-certificate-v3",
                sources=self.source_bindings,
            )
            self.created_store = store_path
        descriptor = {
            "schema": OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA,
            "application_id": OPENPARF_ATOMIC_PLACEMENT_DATABASE_APPLICATION_ID,
            "user_version": OPENPARF_ATOMIC_PLACEMENT_DATABASE_USER_VERSION,
            "file": store_path.name,
            "bytes": store_path.stat().st_size,
            "sha256": digest,
            "clusters": self.cluster_count,
            "assignments": self.assignment_count,
            "logical_sha256": logical_sha256,
        }
        compact["cluster_storage"] = descriptor
        temporary_selector = self.selector_path.with_name(
            f".{self.selector_path.name}.{os.getpid()}.tmp"
        )
        try:
            write_json(temporary_selector, compact, compact=True)
            with temporary_selector.open("rb") as stream:
                os.fsync(stream.fileno())
            require_store_sources_unchanged(self.source_bindings)
            os.replace(temporary_selector, self.selector_path)
            self.selector_committed = True
            _fsync_directory(self.selector_path.parent)
            for candidate in self.selector_path.parent.glob(
                f"{self.selector_path.stem}.*"
                f"{_PLACEMENT_CERTIFICATE_STORE_SUFFIX}"
            ):
                if (
                    candidate.is_symlink()
                    or candidate.resolve() == store_path.resolve()
                ):
                    continue
                if (
                    self.previous_store is None
                    or candidate.resolve() != self.previous_store.resolve()
                ) and not _valid_unreferenced_certificate_store(
                    self.selector_path, candidate
                ):
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    pass
            _fsync_directory(self.selector_path.parent)
            return compact
        finally:
            temporary_selector.unlink(missing_ok=True)
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        database = self.database
        self.database = None
        if database is not None:
            database.close()
        temporary_path = self.temporary_path
        self.temporary_path = None
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if self.created_store is not None and not self.selector_committed:
            self.created_store.unlink(missing_ok=True)
        self.lease.close()

    def __del__(self) -> None:
        self.close()

def write_xilinx_openparf_atomic_placement_certificate(
    path: Path, certificate: Mapping[str, Any], *, source_bindings=()
) -> Dict[str, Any]:
    """Write a compact certificate whose sidecar owns assignment rows."""

    clusters = certificate.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError("OpenPARF atomic placement clusters are invalid")
    builder = _AtomicPlacementCertificateBuilder(path, source_bindings=source_bindings)
    try:
        for cluster in clusters:
            builder.append(cluster)
        return builder.publish(certificate)
    except BaseException:
        builder.close()
        raise

def load_xilinx_openparf_atomic_placement_clusters(
    path: Path, certificate: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Explicit compatibility materialization of certified cluster rows."""

    return list(iter_xilinx_openparf_atomic_placement_clusters(
        path, certificate, allow_legacy=True
    ))

def _iter_placement_database_clusters(
    database: sqlite3.Connection, descriptor: Mapping[str, Any],
) -> Iterator[Dict[str, Any]]:
    cluster_total = 0
    assignment_total = 0
    rows = database.execute(
        "SELECT c.cluster_index,c.cluster,c.site,c.site_type,c.x,c.y,"
        "a.assignment_index,a.instance,a.cell_type,a.bel,a.physical_site,"
        "a.placement_mode,a.source_cluster,a.bram_tile_group "
        "FROM clusters c LEFT JOIN assignments a "
        "ON a.cluster_index=c.cluster_index "
        "ORDER BY c.cluster_index,a.assignment_index"
    )
    current_index = None
    current: Optional[Dict[str, Any]] = None
    expected_assignment_index = 0
    for row in rows:
        if row[0] != current_index:
            if current is not None:
                yield current
            if row[0] != cluster_total:
                raise ValidationError(
                    "OpenPARF atomic placement cluster ordering is invalid"
                )
            current_index = row[0]
            current = {
                "cluster": row[1], "site": row[2],
                "site_type": row[3], "x": row[4], "y": row[5],
                "assignments": [],
            }
            cluster_total += 1
            expected_assignment_index = 0
        if current is None or row[6] is None:
            raise ValidationError(
                "OpenPARF atomic placement database has an empty cluster"
            )
        if row[6] != expected_assignment_index:
            raise ValidationError(
                "OpenPARF atomic placement assignment ordering is invalid"
            )
        expected_assignment_index += 1
        current["assignments"].append({
            "instance": row[7], "cell_type": row[8], "bel": row[9],
            "physical_site": row[10], "placement_mode": row[11],
            "source_cluster": row[12],
            "bram_tile_group": (
                json.loads(row[13]) if row[13] is not None else None
            ),
        })
        assignment_total += 1
    if current is not None:
        yield current
    if (
        cluster_total != descriptor["clusters"]
        or assignment_total != descriptor["assignments"]
    ):
        raise ValidationError(
            "OpenPARF atomic placement database coverage is invalid"
        )

class XilinxOpenparfAtomicPlacementView(MappingABC[str, Any]):
    """Selector and ordered rows held under one shared generation lease."""

    def __init__(self, path: Path, *, allow_legacy: bool = False) -> None:
        self.path = path
        self.lease = _PlacementCertificateLease(path, exclusive=False)
        self.database: Optional[sqlite3.Connection] = None
        self.store_path: Optional[Path] = None
        self.store_generation: Optional[tuple[int, ...]] = None
        self.closed = False
        try:
            if path.is_symlink():
                raise ValidationError(
                    "OpenPARF atomic placement certificate is a symbolic link"
                )
            self._source_bindings = [
                bind_store_source(path, "openparf-placement-selector-v2")
            ]
            try:
                selector = read_json(path)
            except (OSError, ValueError) as error:
                raise ValidationError(
                    "OpenPARF atomic placement selector is invalid"
                ) from error
            if not isinstance(selector, Mapping):
                raise ValidationError(
                    "OpenPARF atomic placement selector is invalid"
                )
            self.selector = selector
            inline = selector.get("clusters")
            if inline is not None:
                if not allow_legacy or not isinstance(inline, list) or not inline:
                    raise ValidationError(
                        "inline OpenPARF placement clusters require explicit "
                        "legacy mode"
                    )
                self.inline = inline
                self.descriptor = None
                self.legacy = True
                self.require_source_unchanged()
                return
            self.inline = None
            descriptor = selector.get("cluster_storage")
            self.legacy = (
                isinstance(descriptor, Mapping)
                and descriptor.get("schema")
                == _LEGACY_OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA
            )
            if self.legacy:
                if (
                    not allow_legacy
                    or not isinstance(descriptor, Mapping)
                    or set(descriptor) != {
                        "schema", "file", "bytes", "sha256", "clusters",
                        "assignments",
                    }
                    or not isinstance(descriptor.get("file"), str)
                    or Path(descriptor["file"]).name != descriptor["file"]
                    or any(
                        isinstance(descriptor.get(key), bool)
                        or not isinstance(descriptor.get(key), int)
                        or descriptor[key] <= 0
                        for key in ("bytes", "clusters", "assignments")
                    )
                    or not isinstance(descriptor.get("sha256"), str)
                    or len(descriptor["sha256"]) != 64
                    or any(
                        c not in "0123456789abcdef"
                        for c in descriptor["sha256"]
                    )
                ):
                    raise ValidationError(
                        "legacy OpenPARF placement database requires explicit "
                        "compatibility mode"
                    )
                unresolved = path.parent / descriptor["file"]
                if unresolved.is_symlink():
                    raise ValidationError(
                        "OpenPARF atomic placement database is a symbolic link"
                    )
                database_path = unresolved.resolve(strict=True)
                self._source_bindings.append(
                    bind_store_source(database_path, "physical-input-document-v1")
                )
                if (
                    database_path.parent != path.parent.resolve()
                    or database_path.stat().st_size != descriptor["bytes"]
                    or _sha256(database_path) != descriptor["sha256"]
                ):
                    raise ValidationError(
                        "OpenPARF atomic placement database seal is invalid"
                    )
            else:
                database_path = _validate_atomic_placement_descriptor(
                    path, descriptor, source_bindings=self._source_bindings
                )
            assert isinstance(descriptor, Mapping)
            self.descriptor = descriptor
            self.store_path = database_path
            self.store_generation = self._source_bindings[-1].generation
            database = sqlite3.connect(
                immutable_sqlite_uri(database_path), uri=True
            )
            self.database = database
            header = dict(selector)
            header.pop("cluster_storage", None)
            _validate_placement_database_contract(
                database, descriptor=descriptor, header=header,
                legacy=self.legacy,
            )
            self.require_source_unchanged()
        except BaseException:
            self.close()
            raise

    def bound_store_sources(self):
        self.require_source_unchanged()
        return tuple(self._source_bindings)

    def require_source_unchanged(self) -> None:
        if self.closed:
            raise ValidationError("OpenPARF atomic placement view is closed")
        require_store_sources_unchanged(self._source_bindings)

    def __getitem__(self, key: str) -> Any:
        return self.selector[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.selector)

    def __len__(self) -> int:
        return len(self.selector)

    def iter_clusters(self) -> Iterator[Dict[str, Any]]:
        if self.closed:
            raise ValidationError("OpenPARF atomic placement view is closed")
        if self.inline is not None:
            for cluster in self.inline:
                if not isinstance(cluster, Mapping):
                    raise ValidationError(
                        "OpenPARF atomic placement cluster is invalid"
                    )
                yield dict(cluster)
            return
        assert self.database is not None and self.descriptor is not None
        yield from _iter_placement_database_clusters(
            self.database, self.descriptor
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.database is not None:
            self.database.close()
            self.database = None
        self.lease.close()

    def __enter__(self) -> "XilinxOpenparfAtomicPlacementView":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "closed"):
            self.close()

def open_xilinx_openparf_atomic_placement(
    path: Path, *, allow_legacy: bool = False,
) -> XilinxOpenparfAtomicPlacementView:
    return XilinxOpenparfAtomicPlacementView(path, allow_legacy=allow_legacy)

def iter_xilinx_openparf_atomic_placement_clusters(
    path: Path, certificate: Mapping[str, Any], *, allow_legacy: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Stream certified clusters from the normalized placement sidecar.

    The compatibility loader above intentionally returns a list.  Production
    bridge code uses this iterator so the certificate is not resident beside
    the packed and placement trees that it is used to construct.
    """

    view = open_xilinx_openparf_atomic_placement(
        path, allow_legacy=allow_legacy
    )
    try:
        if dict(view) != dict(certificate):
            raise ValidationError(
                "OpenPARF atomic placement selector changed before leased read"
            )
        yield from view.iter_clusters()
    finally:
        view.close()

def _site_database_path(
    name_map_path: Path, name_map: Mapping[str, Any]
) -> Tuple[Path, int]:
    descriptor = name_map.get("site_database")
    if not isinstance(descriptor, Mapping):
        raise ValidationError("OpenPARF atomic name map has no site database")
    if descriptor.get("schema") != XILINX_OPENPARF_SITE_DATABASE_SCHEMA:
        raise ValidationError("OpenPARF atomic site database schema is invalid")
    relative = descriptor.get("file")
    count = descriptor.get("sites")
    if (
        not isinstance(relative, str) or not relative
        or Path(relative).name != relative
        or not isinstance(count, int) or isinstance(count, bool) or count <= 0
    ):
        raise ValidationError("OpenPARF atomic site database descriptor is invalid")
    path = name_map_path.parent / relative
    if not path.is_file():
        raise ValidationError("OpenPARF atomic site database is missing")
    return path, count

def load_xilinx_openparf_sites(
    name_map_path: Path,
    *,
    expected_name_map_schema: str,
    coordinates: Optional[Sequence[Tuple[int, int]]] = None,
    resources: Optional[Sequence[str]] = None,
    mapped_sha256: Optional[str] = None,
    packed_sha256: Optional[str] = None,
    architecture_sha256: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read only requested site rows from the normalized placement contract."""

    name_map = (
        read_xilinx_openparf_atomic_name_map(
            name_map_path,
            mapped_sha256=mapped_sha256,
            packed_sha256=packed_sha256,
            architecture_sha256=architecture_sha256,
        )
        if expected_name_map_schema == OPENPARF_ATOMIC_NAME_MAP_SCHEMA
        else read_json(name_map_path)
    )
    try:
        if name_map.get("schema") != expected_name_map_schema:
            raise ValidationError("OpenPARF name map is invalid")
        database_path, expected_count = _site_database_path(name_map_path, name_map)
    finally:
        close_name_map = getattr(name_map, "close", None)
        if callable(close_name_map):
            close_name_map()
    requested = {
        (int(coordinate[0]), int(coordinate[1])) for coordinate in coordinates or []
    }
    resource_filter = {str(resource) for resource in resources or []}
    uri = immutable_sqlite_uri(database_path)
    with closing(sqlite3.connect(uri, uri=True)) as database, database:
        metadata = dict(database.execute("SELECT key, value FROM metadata"))
        if (
            metadata.get("schema") != XILINX_OPENPARF_SITE_DATABASE_SCHEMA
            or metadata.get("site_count") != str(expected_count)
        ):
            raise ValidationError("OpenPARF atomic site database metadata is invalid")
        if requested:
            database.execute(
                "CREATE TEMP TABLE requested ("
                "dense_x INTEGER, dense_y INTEGER, PRIMARY KEY(dense_x, dense_y)) "
                "WITHOUT ROWID"
            )
            database.executemany(
                "INSERT INTO requested VALUES (?, ?)", sorted(requested)
            )
        elif resource_filter:
            database.execute(
                "CREATE TEMP TABLE requested ("
                "dense_x INTEGER, dense_y INTEGER, PRIMARY KEY(dense_x, dense_y)) "
                "WITHOUT ROWID"
            )
            placeholders = ",".join("?" for _ in resource_filter)
            database.execute(
                "INSERT INTO requested "
                "SELECT DISTINCT dense_x, dense_y FROM physical_sites "
                f"WHERE resource IN ({placeholders})",
                sorted(resource_filter),
            )
        site_query = (
            "SELECT s.dense_x, s.dense_y, s.physical_x, s.physical_y, "
            "s.placement_x, s.placement_y, s.site FROM sites s "
            "JOIN requested r USING(dense_x, dense_y) "
            "ORDER BY s.dense_x, s.dense_y"
            if requested or resource_filter else
            "SELECT dense_x, dense_y, physical_x, physical_y, placement_x, "
            "placement_y, site FROM sites ORDER BY dense_x, dense_y"
        )
        entries = {
            (row[0], row[1]): {
                "dense_x": row[0], "dense_y": row[1],
                "physical_x": row[2], "physical_y": row[3],
                "placement_x": row[4], "placement_y": row[5],
                "site": row[6], "resources": {}, "physical_sites": {},
            }
            for row in database.execute(site_query)
        }
        suffix = (
            " JOIN requested q USING(dense_x, dense_y)"
            if requested or resource_filter else ""
        )
        for dense_x, dense_y, resource, capacity in database.execute(
            "SELECT r.dense_x, r.dense_y, r.resource, r.capacity "
            f"FROM resources r{suffix} ORDER BY r.dense_x, r.dense_y, r.resource"
        ):
            entries[(dense_x, dense_y)]["resources"][resource] = capacity
        for dense_x, dense_y, resource, physical_site, _slot in database.execute(
            "SELECT p.dense_x, p.dense_y, p.resource, p.physical_site, p.slot "
            f"FROM physical_sites p{suffix} "
            "ORDER BY p.dense_x, p.dense_y, p.resource, p.slot"
        ):
            entries[(dense_x, dense_y)]["physical_sites"].setdefault(
                resource, []
            ).append(physical_site)
    if requested and set(entries) != requested:
        raise ValidationError("OpenPARF atomic placement uses an unknown site")
    return list(entries.values())

def load_xilinx_openparf_atomic_sites(
    name_map_path: Path,
    *,
    coordinates: Optional[Sequence[Tuple[int, int]]] = None,
    resources: Optional[Sequence[str]] = None,
    mapped_sha256: Optional[str] = None,
    packed_sha256: Optional[str] = None,
    architecture_sha256: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read selected sites from an atomic OpenPARF placement contract."""

    return load_xilinx_openparf_sites(
        name_map_path,
        expected_name_map_schema=OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        coordinates=coordinates,
        resources=resources,
        mapped_sha256=mapped_sha256,
        packed_sha256=packed_sha256,
        architecture_sha256=architecture_sha256,
    )

def _load_physical_macro_groups(
    name_map_path: Path, name_map: Mapping[str, Any]
) -> List[Mapping[str, Any]]:
    descriptor = name_map.get("physical_macro_constraints")
    if descriptor is None:
        return []
    if not isinstance(descriptor, Mapping):
        raise ValidationError("typed hardblock constraint descriptor is invalid")
    relative = descriptor.get("file")
    count = descriptor.get("groups")
    if (
        descriptor.get("schema") != OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA
        or not isinstance(relative, str) or Path(relative).name != relative
        or not isinstance(count, int) or isinstance(count, bool) or count <= 0
    ):
        raise ValidationError("typed hardblock constraint descriptor is invalid")
    contract = read_json(name_map_path.parent / relative)
    groups = contract.get("groups")
    raw_window_sets = contract.get("window_sets", [])
    raw_site_chain_sets = contract.get("site_chain_sets", [])
    if (
        contract.get("schema") != OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA
        or contract.get("status") != "pass"
        or not isinstance(groups, list) or len(groups) != count
        or not isinstance(raw_window_sets, list)
        or not isinstance(raw_site_chain_sets, list)
        or contract.get("site_database") != name_map.get("site_database")
    ):
        raise ValidationError("typed hardblock constraint contract is invalid")
    window_sets: Dict[str, Mapping[str, Any]] = {}
    for item in raw_window_sets:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in window_sets
            or item.get("resource") not in _HARD_RESOURCES.values()
            or not isinstance(item.get("windows"), list)
            or not item["windows"]
        ):
            raise ValidationError("typed hardblock shared window set is invalid")
        window_sets[item["id"]] = item
    site_chain_sets: Dict[str, Mapping[str, Any]] = {}
    for item in raw_site_chain_sets:
        chains = item.get("chains") if isinstance(item, Mapping) else None
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in site_chain_sets
            or item.get("site_resource") != "LUT"
            or not isinstance(chains, list)
            or not chains
            or any(
                not isinstance(chain, list) or len(chain) < 2
                or not all(isinstance(site, str) and site for site in chain)
                or len(chain) != len(set(chain))
                for chain in chains
            )
        ):
            raise ValidationError("typed hardblock site-chain set is invalid")
        site_chain_sets[item["id"]] = item
    result = []
    referenced = set()
    referenced_site_chains = set()
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValidationError("typed hardblock group is invalid")
        chain_template = group.get("chain_template")
        if chain_template is not None:
            chain_set_id = (
                chain_template.get("chain_set")
                if isinstance(chain_template, Mapping) else None
            )
            chain_length = (
                chain_template.get("chain_length")
                if isinstance(chain_template, Mapping) else None
            )
            chain_set = site_chain_sets.get(chain_set_id)
            expected_count = (
                sum(max(0, len(chain) - chain_length + 1)
                    for chain in chain_set["chains"])
                if chain_set is not None
                and isinstance(chain_length, int)
                and not isinstance(chain_length, bool)
                and chain_length >= 2 else 0
            )
            if (
                group.get("kind") != "site_cascade"
                or group.get("resource") != "SLICE_MACRO"
                or not isinstance(chain_template, Mapping)
                or chain_template.get("kind") != "directed-site-chain/v1"
                or chain_set is None
                or expected_count <= 0
                or group.get("window_count") != expected_count
                or "windows" in group
                or "window_template" in group
                or group.get("window_set") is not None
            ):
                raise ValidationError("typed hardblock site-chain reference is invalid")
            referenced_site_chains.add(chain_set_id)
            result.append({**group, "site_chain_set": chain_set})
            continue
        window_set_id = group.get("window_set")
        if window_set_id is None:
            result.append(group)
            continue
        window_set = window_sets.get(window_set_id)
        if (
            window_set is None
            or group.get("kind") != "singleton"
            or group.get("resource") != window_set.get("resource")
            or "windows" in group
            or "window_template" in group
            or group.get("window_count") != len(window_set["windows"])
        ):
            raise ValidationError("typed hardblock window-set reference is invalid")
        referenced.add(window_set_id)
        result.append({**group, "windows": window_set["windows"]})
    if referenced != set(window_sets):
        raise ValidationError("typed hardblock contract has an unused window set")
    if referenced_site_chains != set(site_chain_sets):
        raise ValidationError("typed hardblock contract has an unused site-chain set")
    return result

def _select_module(
    mapped: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any]]:
    modules = mapped.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValidationError("mapped JSON modules are invalid")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, Mapping):
            raise ValidationError(f"mapped JSON has no top module {top!r}")
        return top, module
    selected = [
        (name, module)
        for name, module in modules.items()
        if isinstance(module, Mapping)
        and str(module.get("attributes", {}).get("top", "0")) not in {"", "0"}
    ]
    if len(selected) == 1:
        return selected[0]
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        if isinstance(module, Mapping):
            return str(name), module
    raise ValidationError("mapped JSON top module is ambiguous")

def _pin_bit(cell: Mapping[str, Any], port: str) -> Any:
    connections = cell.get("connections")
    values = connections.get(port, []) if isinstance(connections, Mapping) else []
    if not isinstance(values, list) or len(values) > 1:
        raise ValidationError(f"cell port {port!r} is not a scalar pin")
    return values[0] if values else None

def _control_tuple(cell_type: str, cell: Mapping[str, Any]) -> Tuple[Any, Any, Any]:
    parameters = cell.get("parameters")
    control_mode = (
        cell_type,
        _FF_SR[cell_type],
        _pin_bit(cell, _FF_SR[cell_type]),
        tuple(
            (str(key), json.dumps(value, sort_keys=True, separators=(",", ":")))
            for key, value in sorted(parameters.items()) if str(key).startswith("IS_")
        ) if isinstance(parameters, Mapping) else (),
    )
    return (
        _pin_bit(cell, _FF_CLOCK),
        control_mode,
        _pin_bit(cell, _FF_ENABLE),
    )

def legalize_xilinx_openparf_ff_control_sets(
    placement_path: Path,
    name_map_path: Path,
    mapped_path: Path,
    output_path: Path,
    *,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Repair only native FF slots that violate UltraScale+ control sets.

    OpenPARF's generic direct legalizer models FF capacity but not the shared
    CK/SR and lane-local CE wires of an UltraScale+ half-slice.  Preserve every
    already-compatible FF and all non-FF atoms, then move only conflicting FFs
    to the nearest compatible half-slice.  The independent placement importer
    still rechecks the complete result after this bounded legalization pass.
    """

    mapped_digest = _sha256(mapped_path)
    packed_digest = _sha256(packed_path) if packed_path is not None else None
    name_map = read_xilinx_openparf_atomic_name_map(
        name_map_path,
        mapped_sha256=mapped_digest,
        packed_sha256=packed_digest,
    )
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    try:
        if name_map.get("schema") != OPENPARF_ATOMIC_NAME_MAP_SCHEMA:
            raise ValidationError("OpenPARF atomic name map is invalid")
        name_map_top = name_map.get("top")
        atom_sequence = name_map.get("atoms", [])
        atoms = {
            item["openparf"]: item for item in atom_sequence
            if isinstance(item, Mapping) and isinstance(item.get("openparf"), str)
        }
        if len(atoms) != len(atom_sequence) or not atoms:
            raise ValidationError("OpenPARF atomic name map has duplicate atoms")
    finally:
        close_name_map = getattr(name_map, "close", None)
        if callable(close_name_map):
            close_name_map()
    _top, module = _select_module(mapped, name_map_top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")

    raw: Dict[str, Tuple[int, int, int]] = {}
    with placement_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            fields = line.strip().split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) not in {4, 5} or fields[0] not in atoms:
                raise ImportError(
                    f"{placement_path}:{line_number}: invalid atomic placement row"
                )
            try:
                numeric = [float(value) for value in fields[1:4]]
            except ValueError as error:
                raise ImportError("OpenPARF atomic placement is non-numeric") from error
            if any(not math.isfinite(value) or not value.is_integer() for value in numeric):
                raise ValidationError("OpenPARF atomic placement is not discrete")
            if fields[0] in raw:
                raise ValidationError("OpenPARF atomic placement duplicates an atom")
            raw[fields[0]] = tuple(int(value) for value in numeric)
    if set(raw) != set(atoms):
        raise ValidationError("OpenPARF atomic placement does not cover every atom")

    ff_sites = {
        (site["dense_x"], site["dense_y"]): site
        for site in load_xilinx_openparf_atomic_sites(
            name_map_path, resources=["FF"],
            mapped_sha256=mapped_digest,
            packed_sha256=packed_digest,
        )
        if site.get("resources", {}).get("FF", 0) >= 16
        and len(site.get("physical_sites", {}).get("FF", [])) == 1
    }
    if not ff_sites:
        raise ValidationError("OpenPARF site database has no complete FF sites")
    exclusive_sites = {
        (raw[name][0], raw[name][1])
        for name, atom in atoms.items()
        if atom.get("resource") == "CARRY8"
    }

    ff_by_half: Dict[Tuple[int, int, int], List[str]] = defaultdict(list)
    for name, atom in atoms.items():
        if atom.get("resource") != "FF":
            continue
        x, y, z = raw[name]
        if (x, y) not in ff_sites or not 0 <= z < 16:
            raise ValidationError("OpenPARF FF placement uses an invalid slot")
        ff_by_half[(x, y, 0 if z < 8 else 1)].append(name)

    states: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    moved: List[str] = []
    illegal_halves = 0
    for half_key, names in sorted(ff_by_half.items()):
        scored: Counter = Counter()
        by_signature: Dict[Tuple[Any, Any], Dict[int, Counter]] = defaultdict(
            lambda: {0: Counter(), 1: Counter()}
        )
        for name in names:
            atom = atoms[name]
            cell = cells[atom["instance"]]
            clock, sr, enable = _control_tuple(atom["cell_type"], cell)
            lane = raw[name][2] % 2
            by_signature[(clock, sr)][lane][enable] += 1
        for signature, lanes in by_signature.items():
            scored[signature] = sum(
                max(counts.values(), default=0) for counts in lanes.values()
            )
        signature = min(scored, key=lambda item: (-scored[item], repr(item)))
        lane_enable = {
            lane: min(counts, key=lambda item: (-counts[item], repr(item)))
            for lane, counts in by_signature[signature].items() if counts
        }
        state = {
            "signature": signature,
            "lane_enable": lane_enable,
            "occupied": set(),
        }
        states[half_key] = state
        half_moved = False
        for name in sorted(names):
            atom = atoms[name]
            clock, sr, enable = _control_tuple(
                atom["cell_type"], cells[atom["instance"]]
            )
            z = raw[name][2]
            if (clock, sr) == signature and lane_enable.get(z % 2) == enable:
                state["occupied"].add(z)
            else:
                moved.append(name)
                half_moved = True
        if half_moved:
            illegal_halves += 1

    def candidate_at(
        coordinate: Tuple[int, int], half: int, signature: Tuple[Any, Any],
        enable: Any, preferred_lane: int,
    ) -> Optional[int]:
        if coordinate in exclusive_sites:
            return None
        key = (coordinate[0], coordinate[1], half)
        state = states.get(key)
        if state is None:
            state = {"signature": None, "lane_enable": {}, "occupied": set()}
            states[key] = state
        if state["signature"] not in {None, signature}:
            return None
        for lane in (preferred_lane, 1 - preferred_lane):
            lane_control = state["lane_enable"].get(lane)
            if lane_control not in {None, enable}:
                continue
            slots = [half * 8 + lane + 2 * offset for offset in range(4)]
            free = next((slot for slot in slots if slot not in state["occupied"]), None)
            if free is None:
                continue
            state["signature"] = signature
            state["lane_enable"][lane] = enable
            state["occupied"].add(free)
            return free
        return None

    max_x = max(x for x, _y in ff_sites)
    min_x = min(x for x, _y in ff_sites)
    max_y = max(y for _x, y in ff_sites)
    min_y = min(y for _x, y in ff_sites)
    total_distance = 0
    maximum_distance = 0
    for name in sorted(moved, key=lambda item: (*raw[item][:2], item)):
        atom = atoms[name]
        clock, sr, enable = _control_tuple(
            atom["cell_type"], cells[atom["instance"]]
        )
        origin_x, origin_y, origin_z = raw[name]
        assigned = None
        maximum_radius = (
            max(abs(origin_x - min_x), abs(origin_x - max_x))
            + max(abs(origin_y - min_y), abs(origin_y - max_y))
        )
        for radius in range(maximum_radius + 1):
            for delta_y in range(-radius, radius + 1):
                delta_x = radius - abs(delta_y)
                xs = (
                    (origin_x,) if delta_x == 0
                    else (origin_x - delta_x, origin_x + delta_x)
                )
                for x in xs:
                    coordinate = (x, origin_y + delta_y)
                    if coordinate not in ff_sites:
                        continue
                    original_half = 0 if origin_z < 8 else 1
                    for half in (original_half, 1 - original_half):
                        z = candidate_at(
                            coordinate, half, (clock, sr), enable,
                            origin_z % 2,
                        )
                        if z is not None:
                            assigned = (coordinate[0], coordinate[1], z)
                            break
                    if assigned is not None:
                        break
                if assigned is not None:
                    break
            if assigned is not None:
                total_distance += radius
                maximum_distance = max(maximum_distance, radius)
                break
        if assigned is None:
            raise ValidationError(
                "UltraScale+ FF control-set legalization exhausted all slice sites"
            )
        raw[name] = assigned

    before_sha256 = _sha256(placement_path)
    if moved:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=placement_path.parent,
            prefix=placement_path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            for name in sorted(raw):
                x, y, z = raw[name]
                stream.write(f"{name} {x} {y} {z}\n")
        os.replace(temporary, placement_path)
    report = {
        "schema": OPENPARF_FF_CONTROL_LEGALIZATION_SCHEMA,
        "status": "pass",
        "provider": "nearest-compatible-ultrascaleplus-half-slice-v1",
        "placement_before_sha256": before_sha256,
        "placement_after_sha256": _sha256(placement_path),
        "ff_atoms": sum(atom.get("resource") == "FF" for atom in atoms.values()),
        "illegal_halves": illegal_halves,
        "moved_ff_atoms": len(moved),
        "total_manhattan_distance": total_distance,
        "maximum_manhattan_distance": maximum_distance,
    }
    write_json(output_path, report, compact=True)
    return report

def _native_bram_candidates(
    coordinate_system: Mapping[str, Any], native: Mapping[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Bind source-sealed BRAM views to the single ArchitectureDB anchor.

    The native artifact is authoritative for tile membership, placement site
    type, site name, BEL, and whole-versus-half exclusion.  The ArchitectureDB
    contributes only the Bookshelf coordinate of its retained RAMB181 anchor.
    """

    coordinates: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for item in coordinate_system.get("sites", []):
        if not isinstance(item, Mapping):
            continue
        physical = item.get("physical_sites", {})
        if not isinstance(physical, Mapping):
            continue
        for resource in ("RAMB18E2", "RAMB36E2"):
            names = physical.get(resource, [])
            if not isinstance(names, list):
                raise ValidationError("BRAM Bookshelf physical-site map is invalid")
            for anchor in names:
                key = (resource, anchor)
                if key in coordinates:
                    raise ValidationError("BRAM anchor appears at multiple coordinates")
                coordinates[key] = item

    result: Dict[str, List[Dict[str, Any]]] = {
        "RAMB18E2": [], "RAMB36E2": [],
    }
    if not any(key[0] in result for key in coordinates):
        return result
    groups = native.get("payload", {}).get("bram_tile_groups", [])
    if not isinstance(groups, list) or not groups:
        raise ValidationError("native constraints have no BRAM tile groups")
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValidationError("native BRAM tile group is invalid")
        anchor = group.get("anchor")
        tile = group.get("tile")
        if not isinstance(anchor, str) or not isinstance(tile, str):
            raise ValidationError("native BRAM tile group identity is invalid")
        for resource, roles in (
            ("RAMB18E2", (("lower", 0), ("upper", 1))),
            ("RAMB36E2", (("whole", 0),)),
        ):
            coordinate = coordinates.get((resource, anchor))
            if coordinate is None:
                continue
            for role, z in roles:
                view = group.get(role)
                if not isinstance(view, Mapping):
                    raise ValidationError("native BRAM tile view is invalid")
                result[resource].append({
                    "anchor": anchor,
                    "bel": view["bel"],
                    "claims": _bram_claims(tile, role),
                    "placement_mode": view["site_type"],
                    "resource": resource,
                    "role": role,
                    "site": view["site"],
                    "tile": tile,
                    "x": coordinate["placement_x"],
                    "y": coordinate["placement_y"],
                    "z": z,
                })
    for resource, values in result.items():
        values.sort(key=lambda item: (item["anchor"], item["z"], item["site"]))
        expected = len(coordinates)
        if resource == "RAMB18E2":
            expected = 2 * sum(key[0] == resource for key in coordinates)
        else:
            expected = sum(key[0] == resource for key in coordinates)
        if len(values) != expected:
            raise ValidationError(
                f"native BRAM candidates do not cover {resource} anchors"
            )
    return result

def _slot_bel(resource: str, z: int, cell_type: str) -> str:
    if not 0 <= z < 16:
        raise ValidationError("OpenPARF atomic placement has an invalid z slot")
    letter = "ABCDEFGH"[z // 2]
    if resource == "LUT":
        if z % 2 == 0:
            raise ValidationError(
                "OpenPARF used an even/paired LUT slot that has no qualified "
                "physical 5LUT mapping"
            )
        if cell_type not in _SLICE_LUT_TYPES:
            raise ValidationError("OpenPARF LUT slot contains a non-LUT primitive")
        return f"{letter}6LUT"
    return f"{letter}FF" if z % 2 == 0 else f"{letter}FF2"

def validate_xilinx_openparf_atomic_placement(
    placement_path: Path,
    name_map_path: Path,
    mapped_path: Path,
    architecture_path: Path,
    output_path: Optional[Path] = None,
    *,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[ArchitectureDB] = None,
    packed_path: Optional[Path] = None,
    materialize_result: bool = True,
    certificate_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate and aggregate native atom placement without fallback."""

    source_bindings = _physical_input_bindings(
        (
            name_map_path,
            mapped_path,
            architecture_path,
            packed_path,
            native_constraints_path,
            provider_manifest_path,
        ),
        mapped_value,
    )
    if output_path is not None:
        validate_store_destination(output_path, source_bindings)
    mapped_digest = _sha256(mapped_path)
    packed_digest = _sha256(packed_path) if packed_path is not None else None
    architecture_digest = _sha256(architecture_path)
    if (native_constraints_path is None) != (provider_manifest_path is None):
        raise ValidationError(
            "OpenPARF placement validation requires both native constraints "
            "and provider manifest"
        )
    name_map = read_xilinx_openparf_atomic_name_map(
        name_map_path,
        mapped_sha256=mapped_digest,
        packed_sha256=packed_digest,
        architecture_sha256=architecture_digest,
    )
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    certificate_builder: Optional[_AtomicPlacementCertificateBuilder] = None
    try:
        if name_map.get("schema") != OPENPARF_ATOMIC_NAME_MAP_SCHEMA:
            raise ValidationError("OpenPARF atomic name map is invalid")
        if not isinstance(name_map, XilinxOpenparfAtomicNameMapView):
            raise ValidationError(
                "production OpenPARF placement validation requires the typed "
                "atomic name map"
            )
        source_bindings += _physical_input_bindings((placement_path,), name_map, mapped)
        name_map_top = name_map.get("top")
        hardblock_groups = _load_physical_macro_groups(name_map_path, name_map)
        carry_cascade_chains = list(name_map.get("carry_cascade_chains", []))
        placement_index = _IndexedAtomicPlacement(placement_path, name_map)
    except BaseException:
        name_map.close()
        raise
    try:
        _top, module = _select_module(mapped, name_map_top)
        cells = module.get("cells")
        if not isinstance(cells, Mapping):
            raise ValidationError("mapped JSON cells are invalid")
        requested_coordinates = placement_index.requested_coordinates()
        site_map = {
            (item["dense_x"], item["dense_y"]): item
            for item in load_xilinx_openparf_atomic_sites(
                name_map_path, coordinates=requested_coordinates,
                mapped_sha256=mapped_digest,
                packed_sha256=packed_digest,
                architecture_sha256=architecture_digest,
            )
        }
        if (
            not isinstance(carry_cascade_chains, Sequence)
            or isinstance(carry_cascade_chains, (str, bytes))
            or any(
            not isinstance(chain, Mapping)
            or chain.get("cell_type") != "CARRY8"
            or not isinstance(chain.get("instances"), list)
            or len(chain["instances"]) < 2
            for chain in carry_cascade_chains
            )
        ):
            raise ValidationError("OpenPARF CARRY8 cascade contract is invalid")
        native_hardblock_groups = [
            group for group in hardblock_groups
            if group.get("resource") in _HARD_RESOURCES.values()
        ]
        native = None
        native_report = None
        bram_view_by_slot: Dict[Tuple[int, int, str, int], Mapping[str, Any]] = {}
        if native_hardblock_groups or carry_cascade_chains:
            if native_constraints_path is None or provider_manifest_path is None:
                raise ValidationError(
                    "typed hardblock placement validation requires native constraints"
                )
            native, native_report = load_xilinx_native_device_constraints(
                native_constraints_path,
                architecture_path=architecture_path,
                provider_manifest_path=provider_manifest_path,
            )
            bram_sites = load_xilinx_openparf_atomic_sites(
                name_map_path, resources=("RAMB18E2", "RAMB36E2"),
                mapped_sha256=mapped_digest,
                packed_sha256=packed_digest,
                architecture_sha256=architecture_digest,
            )
            bram_candidates = _native_bram_candidates({"sites": bram_sites}, native)
            dense_by_anchor = {}
            for entry in bram_sites:
                dense_x, dense_y = entry["dense_x"], entry["dense_y"]
                physical = entry.get("physical_sites", {})
                if not isinstance(physical, Mapping):
                    continue
                for resource in ("RAMB18E2", "RAMB36E2"):
                    for anchor in physical.get(resource, []):
                        dense_by_anchor[(resource, anchor)] = (dense_x, dense_y)
            for resource, candidates in bram_candidates.items():
                for candidate in candidates:
                    dense = dense_by_anchor.get((resource, candidate["anchor"]))
                    if dense is None:
                        raise ValidationError("native BRAM candidate has no Bookshelf anchor")
                    key = (dense[0], dense[1], resource, int(candidate["z"]))
                    if key in bram_view_by_slot:
                        raise ValidationError("native BRAM slot mapping is ambiguous")
                    bram_view_by_slot[key] = candidate
        try:
            for atom, (x, y, z) in placement_index.iter_joined():
                site_entry = site_map.get((x, y))
                if site_entry is None:
                    raise ValidationError("OpenPARF atomic placement uses an unknown site")
                cell_type = atom["cell_type"]
                resource = atom.get("resource")
                candidate = None
                resources = site_entry.get("resources")
                if (
                    not isinstance(resources, Mapping)
                    or (
                        resource not in {"LUT", "FF", "CARRY8"}
                        | set(_MUX_RESOURCES.values())
                        and resources.get(resource, 0) < 1
                    )
                ):
                    raise ValidationError(
                        "OpenPARF atomic placement uses a site without the required resource"
                    )
                if resource in {"LUT", "FF"}:
                    if not isinstance(resources, Mapping) or resource not in resources:
                        raise ValidationError(
                            "OpenPARF atomic placement uses a site without the required resource"
                        )
                    bel_name = _slot_bel(resource, z, cell_type)
                elif resource in _MUX_RESOURCES.values():
                    roles = _MUX_BELS.get(cell_type)
                    if roles is None or z < 0 or z >= len(roles):
                        raise ValidationError(
                            "OpenPARF MUX placement uses an invalid BEL slot"
                        )
                    bel_name = roles[z]
                    physical_sites = site_entry.get("physical_sites", {}).get(resource)
                    if not isinstance(physical_sites, list) or len(physical_sites) != 1:
                        raise ValidationError(
                            "OpenPARF MUX placement has no unique physical slice"
                        )
                    site_name = physical_sites[0]
                elif resource == "CARRY8":
                    if z != 0 or cell_type != "CARRY8":
                        raise ValidationError(
                            "OpenPARF CARRY8 placement uses an invalid resource slot"
                        )
                    bel_name = "CARRY8"
                    physical_sites = site_entry.get("physical_sites", {}).get(resource)
                    if not isinstance(physical_sites, list) or len(physical_sites) != 1:
                        raise ValidationError(
                            "OpenPARF CARRY8 placement has no unique physical slice"
                        )
                    site_name = physical_sites[0]
                elif resource in {"RAMB18E2", "RAMB36E2"} and bram_view_by_slot:
                    candidate = bram_view_by_slot.get((x, y, resource, z))
                    if candidate is None:
                        raise ValidationError(
                            "OpenPARF BRAM placement uses an uncertified tile view"
                        )
                    site_name = candidate["site"]
                    bel_name = candidate["bel"]
                elif resource in _HARD_RESOURCES.values():
                    physical_sites = site_entry.get("physical_sites", {}).get(resource)
                    if (
                        not isinstance(physical_sites, list)
                        or z < 0 or z >= len(physical_sites)
                    ):
                        raise ValidationError(
                            "OpenPARF hard-resource placement uses an invalid site slot"
                        )
                    site_name = physical_sites[z]
                    bel_name = None
                else:
                    raise ValidationError("OpenPARF atomic placement has an unknown resource")
                if resource in {"LUT", "FF"}:
                    physical_sites = site_entry.get("physical_sites", {}).get(resource)
                    if not isinstance(physical_sites, list) or len(physical_sites) != 1:
                        raise ValidationError(
                            "OpenPARF logic placement has no unique physical slice"
                        )
                    site_name = physical_sites[0]
                placement_index.claim_occupancy(site_name, resource, z)
                anchor_names = site_entry.get("physical_sites", {}).get(resource)
                anchor_name = (
                    anchor_names[0]
                    if resource in {"RAMB18E2", "RAMB36E2"} and bram_view_by_slot
                    and isinstance(anchor_names, list) and len(anchor_names) == 1
                    else site_name
                )
                site = architecture.site_named(anchor_name)
                compatible = [
                    bel for bel in site["bels"]
                    if cell_type in bel["compatible_cells"]
                    and (bel_name is None or bel["name"] == bel_name)
                ] if site is not None else []
                if len(compatible) != 1:
                    raise ValidationError(
                        "OpenPARF atomic placement has no unique compatible physical BEL"
                    )
                physical_bel = compatible[0]
                placement_index.append_normalized(
                    atom,
                    z=z,
                    x=site_entry["placement_x"],
                    y=site_entry["placement_y"],
                    site=site_name,
                    anchor_site=anchor_name,
                    bel=physical_bel["name"],
                    placement_mode=(
                        candidate["placement_mode"]
                        if resource in {"RAMB18E2", "RAMB36E2"} and bram_view_by_slot
                        else physical_bel.get("placement_mode", site["type"])
                    ),
                    bram_tile_group=(
                        {
                            "anchor": candidate["anchor"],
                            "claims": list(candidate["claims"]),
                            "role": candidate["role"],
                            "tile": candidate["tile"],
                        }
                        if candidate is not None else None
                    ),
                )
        except BaseException:
            raise

        checked_native_edges = 0
        if hardblock_groups:
            family_for_resource = {
                "DSP48E2": "DSP_CASCADE", "RAMB36E2": "BRAM_CASCADE",
                "URAM288": "URAM_CASCADE",
            }
            native_families = {
                family.get("kind"): family
                for family in (
                    native.get("payload", {}).get("dedicated_adjacency", [])
                    if native is not None else []
                )
                if isinstance(family, Mapping)
            }
            covered = set()
            occupied_claims = set()
            for group in hardblock_groups:
                if not isinstance(group, Mapping):
                    raise ValidationError("typed hardblock group is invalid")
                resource = group.get("resource")
                instances = group.get("source_instances")
                if (
                    resource not in set(_HARD_RESOURCES.values()) | {"SLICE_MACRO"}
                    or not isinstance(instances, list)
                ):
                    raise ValidationError("typed hardblock group header is invalid")
                if covered.intersection(instances):
                    raise ValidationError("typed hardblock placement ownership overlaps")
                covered.update(instances)
                try:
                    selected = placement_index.instances(instances)
                except KeyError as error:
                    raise ValidationError(
                        "typed hardblock group references an unknown instance"
                    ) from error
                template = group.get("window_template")
                chain_template = group.get("chain_template")
                if group.get("kind") == "site_cascade" and chain_template is not None:
                    unit_members = (
                        chain_template.get("unit_members")
                        if isinstance(chain_template, Mapping) else None
                    )
                    chain_length = (
                        chain_template.get("chain_length")
                        if isinstance(chain_template, Mapping) else None
                    )
                    chain_set = group.get("site_chain_set")
                    if (
                        not isinstance(chain_template, Mapping)
                        or chain_template.get("kind") != "directed-site-chain/v1"
                        or not isinstance(unit_members, list)
                        or not unit_members
                        or not isinstance(chain_length, int)
                        or isinstance(chain_length, bool)
                        or chain_length < 2
                        or len(selected) != chain_length * len(unit_members)
                        or not isinstance(chain_set, Mapping)
                    ):
                        raise ValidationError(
                            "OpenPARF site cascade has an invalid compact template"
                        )
                    units = [
                        selected[index:index + len(unit_members)]
                        for index in range(0, len(selected), len(unit_members))
                    ]
                    unit_sites = []
                    for unit in units:
                        if (
                            len({item["site"] for item in unit}) != 1
                            or not all(
                                member.get("resource") == item["resource"]
                                and member.get("bel") == item["bel"]
                                and isinstance(member.get("z"), (int, float))
                                and not isinstance(member.get("z"), bool)
                                and float(member["z"]) == float(item["z"])
                                for member, item in zip(unit_members, unit)
                            )
                        ):
                            raise ValidationError(
                                "OpenPARF site cascade unit violates its slice template"
                            )
                        unit_sites.append(unit[0]["site"])
                    matching_chains = sum(
                        chain[start:start + chain_length] == unit_sites
                        for chain in chain_set["chains"]
                        for start in range(len(chain) - chain_length + 1)
                    )
                    if matching_chains != 1:
                        raise ValidationError(
                            "OpenPARF site cascade does not match one directed site window"
                        )
                    selected_window = [
                        {
                            **dict(member), "site": item["site"],
                            "x": item["x"], "y": item["y"],
                            "claims": _site_claim(item["site"]),
                        }
                        for unit in units
                        for member, item in zip(unit_members, unit)
                    ]
                elif group.get("kind") == "site_macro" and template is not None:
                    members = (
                        template.get("members")
                        if isinstance(template, Mapping) else None
                    )
                    if (
                        not isinstance(template, Mapping)
                        or template.get("kind") != "same-site-slice/v1"
                        or template.get("site_resource") != "LUT"
                        or not isinstance(group.get("window_count"), int)
                        or isinstance(group.get("window_count"), bool)
                        or group["window_count"] <= 0
                        or not isinstance(members, list)
                        or len(members) != len(selected)
                        or not all(isinstance(member, Mapping) for member in members)
                        or len({item["site"] for item in selected}) != 1
                        or not all(
                            member.get("resource") == item["resource"]
                            and member.get("bel") == item["bel"]
                            and isinstance(member.get("z"), (int, float))
                            and not isinstance(member.get("z"), bool)
                            and float(member["z"]) == float(item["z"])
                            for member, item in zip(members, selected)
                        )
                    ):
                        raise ValidationError(
                            "OpenPARF site macro does not match its compact window template"
                        )
                    selected_window = [
                        {
                            **dict(member), "site": item["site"],
                            "x": item["x"], "y": item["y"],
                            "claims": _site_claim(item["site"]),
                        }
                        for member, item in zip(members, selected)
                    ]
                else:
                    matching_windows = []
                    for window in group.get("windows", []):
                        if not isinstance(window, list) or len(window) != len(selected):
                            continue
                        if not all(isinstance(entry, Mapping) for entry in window):
                            continue
                        if all(
                            entry.get("site") == item["site"]
                            and isinstance(entry.get("z"), (int, float))
                            and not isinstance(entry.get("z"), bool)
                            and float(entry["z"]) == float(item["z"])
                            for entry, item in zip(window, selected)
                        ):
                            matching_windows.append(window)
                    if len(matching_windows) != 1:
                        raise ValidationError(
                            "OpenPARF typed hardblock placement does not match exactly "
                            "one certified window"
                        )
                    selected_window = matching_windows[0]
                claims = []
                for entry in selected_window:
                    raw_claims = entry.get("claims")
                    if (
                        not isinstance(raw_claims, list) or not raw_claims
                        or not all(isinstance(claim, str) and claim for claim in raw_claims)
                    ):
                        raise ValidationError(
                            "OpenPARF typed hardblock window has invalid occupancy claims"
                        )
                    claims.extend(raw_claims)
                unique_claims = set(claims)
                internally_valid = (
                    group.get("kind") == "site_macro"
                    and len({item["site"] for item in selected}) == 1
                    and unique_claims == {"site:" + selected[0]["site"]}
                ) or (
                    group.get("kind") == "site_cascade"
                    and unique_claims == {"site:" + site for site in unit_sites}
                    and len(unit_sites) == len(set(unit_sites))
                ) or len(claims) == len(unique_claims)
                if not internally_valid or occupied_claims.intersection(unique_claims):
                    raise ValidationError(
                        "OpenPARF typed hardblock placement has conflicting occupancy claims"
                    )
                occupied_claims.update(unique_claims)
                sites = [item["site"] for item in selected]
                if group.get("kind") == "cascade" and len(instances) > 1:
                    assert native is not None and native_report is not None
                    if resource not in family_for_resource:
                        raise ValidationError(
                            "typed hardblock cascade uses an unqualified resource"
                        )
                    kind = family_for_resource[resource]
                    require_xilinx_native_constraint_capability(
                        native_report, "dedicated_adjacency." + kind
                    )
                    family = native_families.get(kind, {})
                    native_edges = {
                        edge
                        for chain in family.get("chains", [])
                        for edge in zip(chain, chain[1:])
                    }
                    for edge in zip(sites, sites[1:]):
                        if edge not in native_edges:
                            raise ValidationError(
                                "OpenPARF typed hardblock chain violates native adjacency"
                            )
                        checked_native_edges += 1
            has_site_macro_groups = any(
                group.get("kind") in {"site_macro", "site_cascade"}
                for group in hardblock_groups
            )
            derived_macro_contract = (
                build_xilinx_physical_macro_contract(
                    mapped_path, top=name_map_top, source=mapped
                )
                if has_site_macro_groups else {"site_macros": []}
            )
            expected_mux_members = {
                member["instance"]
                for macro in derived_macro_contract.get("site_macros", [])
                if macro.get("kind") in {"muxf7-cone", "muxf8-cone", "muxf9-cone"}
                for member in macro.get("members", [])
            }
            expected_carry_members = {
                member["instance"]
                for macro in derived_macro_contract.get("site_macros", [])
                if macro.get("kind") == "carry8-lut6_2"
                for member in macro.get("members", [])
            }
            expected = (
                placement_index.hard_resource_instances()
                | expected_mux_members | expected_carry_members
            )
            if covered != expected:
                raise ValidationError("typed hardblock placement coverage is incomplete")

        checked_carry_edges = 0
        if carry_cascade_chains:
            assert native is not None and native_report is not None
            require_xilinx_native_constraint_capability(
                native_report, "dedicated_adjacency.CARRY_NEXT"
            )
            carry_family = next((
                family
                for family in native.get("payload", {}).get("dedicated_adjacency", [])
                if isinstance(family, Mapping) and family.get("kind") == "CARRY_NEXT"
            ), None)
            if not isinstance(carry_family, Mapping):
                raise ValidationError("native constraints contain no CARRY_NEXT family")
            native_carry_edges = {
                edge for chain in carry_family.get("chains", [])
                if isinstance(chain, list) for edge in zip(chain, chain[1:])
            }
            for chain in carry_cascade_chains:
                instances = chain["instances"]
                try:
                    sites = [
                        item["site"] for item in placement_index.instances(instances)
                    ]
                except KeyError as error:
                    raise ValidationError(
                        "CARRY8 cascade references an unknown placed instance"
                    ) from error
                for edge in zip(sites, sites[1:]):
                    if edge not in native_carry_edges:
                        raise ValidationError(
                            "OpenPARF CARRY8 chain "
                            f"{chain.get('id')!r} violates native adjacency at "
                            f"{edge!r}; placed sites are {sites!r}"
                        )
                    checked_carry_edges += 1

        clusters: list[Dict[str, Any]] = []
        if output_path is not None and not materialize_result:
            certificate_builder = _AtomicPlacementCertificateBuilder(
                output_path, source_bindings=source_bindings
            )
        for site_name, items in placement_index.iter_site_groups():
            if sum(item["resource"] == "LUT" for item in items) > 8:
                raise ValidationError(f"site {site_name!r} exceeds physical LUT capacity")
            if sum(item["resource"] == "FF" for item in items) > 16:
                raise ValidationError(f"site {site_name!r} exceeds physical FF capacity")
            carry_items = [item for item in items if item["resource"] == "CARRY8"]
            if carry_items:
                if (
                    len(carry_items) != 1
                    or len(items) != 9
                    or Counter(item["cell_type"] for item in items)
                    != Counter({"CARRY8": 1, DUAL_OUTPUT_LUT_TYPE: 8})
                    or {item["bel"] for item in items}
                    != {"CARRY8", *{f"{letter}6LUT" for letter in "ABCDEFGH"}}
                ):
                    raise ValidationError(
                        f"site {site_name!r} does not contain one exclusive full-slice "
                        "CARRY8/LUT6_2 macro"
                    )
            hard_items = [
                item for item in items if item["resource"] in _HARD_RESOURCES.values()
            ]
            ramb18_items = [
                item for item in hard_items if item["resource"] == "RAMB18E2"
            ]
            if len(hard_items) > 1 and (
                len(hard_items) != len(ramb18_items)
                or len(ramb18_items) > 2
                or len({item["bel"] for item in ramb18_items}) != len(ramb18_items)
            ):
                raise ValidationError(
                    f"site {site_name!r} has incompatible hard-resource occupancy"
                )
            half_cksr: Dict[int, Tuple[Any, Any]] = {}
            quarter_ce: Dict[Tuple[int, int], Any] = {}
            for item in items:
                if item["resource"] != "FF":
                    continue
                clock, sr, enable = _control_tuple(
                    item["cell_type"], cells[item["instance"]]
                )
                half = 0 if item["z"] < 8 else 1
                quarter = (half, item["z"] % 2)
                previous_cksr = half_cksr.setdefault(half, (clock, sr))
                previous_ce = quarter_ce.setdefault(quarter, enable)
                if previous_cksr != (clock, sr) or previous_ce != enable:
                    raise ValidationError(
                        f"site {site_name!r} violates native FF control-set legality"
                    )
            site = architecture.site_named(site_name)
            cluster = {
                "cluster": f"openparf:{site_name}", "site": site_name,
                "site_type": site["type"], "x": site["x"], "y": site["y"],
                "assignments": [
                    {
                        "instance": item["instance"],
                        "cell_type": item["cell_type"], "bel": item["bel"],
                        "placement_mode": item["placement_mode"],
                        "physical_site": item["site"],
                        "bram_tile_group": item["bram_tile_group"],
                        "source_cluster": item["source_cluster"],
                    }
                    for item in sorted(items, key=lambda value: value["instance"])
                ],
            }
            if certificate_builder is not None:
                certificate_builder.append(cluster)
            else:
                clusters.append(cluster)
        atom_count, resource_counts = placement_index.summary()
        result = {
            "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
            "status": "pass", "part": architecture.part,
            "provider": OPENPARF_ATOMIC_PROVIDER,
            "runtime_validation": "unverified",
            "source": {
                "native_placement_sha256": _sha256(placement_path),
                "name_map_sha256": _sha256(name_map_path),
                "mapped_sha256": _sha256(mapped_path),
                "architecture_sha256": _sha256(architecture_path),
            },
            "summary": {
                "atoms": atom_count,
                "occupied_sites": (
                    certificate_builder.cluster_count
                    if certificate_builder is not None else len(clusters)
                ),
                "luts": resource_counts["LUT"],
                "ffs": resource_counts["FF"],
                "hard_resources": dict(sorted(
                    (resource, resource_counts[resource])
                    for resource in _HARD_RESOURCES.values()
                    if resource_counts[resource]
                )),
            },
        }
        carry8_macro_count = resource_counts["CARRY8"]
        if carry8_macro_count:
            result["summary"]["carry8_macros"] = carry8_macro_count
        if packed_digest is not None:
            result["source"]["packed_sha256"] = packed_digest
        if native_constraints_path is not None:
            assert provider_manifest_path is not None
            result["source"].update({
                "native_constraints_sha256": _sha256(native_constraints_path),
                "provider_manifest_sha256": _sha256(provider_manifest_path),
            })
        if checked_carry_edges:
            result["summary"]["native_carry_edges"] = checked_carry_edges
        if hardblock_groups:
            if native_hardblock_groups or carry_cascade_chains:
                assert native_constraints_path is not None
                assert provider_manifest_path is not None
            if checked_native_edges:
                result["summary"]["native_hardblock_edges"] = checked_native_edges
        if certificate_metadata is not None:
            if (
                not isinstance(certificate_metadata, Mapping)
                or set(certificate_metadata)
                - {
                    "ff_control_legalization", "native_convergence",
                    "runtime_validation",
                }
                or (
                    "runtime_validation" in certificate_metadata
                    and certificate_metadata["runtime_validation"]
                    not in {"unverified", "native-openparf"}
                )
            ):
                raise ValidationError(
                    "OpenPARF placement-certificate metadata is invalid"
                )
            result.update(certificate_metadata)
        if certificate_builder is not None:
            result = certificate_builder.publish(result)
            certificate_builder = None
        else:
            result["clusters"] = clusters
            if output_path is not None:
                write_xilinx_openparf_atomic_placement_certificate(
                    output_path, result, source_bindings=source_bindings
                )
        return result
    finally:
        if certificate_builder is not None:
            certificate_builder.close()
        placement_index.close()

from emuflow.physical.openparf.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source, export_xilinx_openparf_atomic
