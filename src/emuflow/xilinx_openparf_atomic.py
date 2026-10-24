"""Narrow atomic LUT/FF plus singleton-hard OpenPARF native adapter.

This is an internal qualification route, not a production placer selection.
It dissolves only ordinary, unconstrained LUT1--LUT6/FD* slice clusters and
lets OpenPARF's native direct legalizer and ISM detailed placer repack them.
Independent singleton DSP48E2/RAMB18E2/RAMB36E2/URAM288 objects use the same run's
native single-site-resource min-cost-flow legalization.
Every unsupported physical relation is rejected before export.  The importer
then independently checks the conservative UltraScale+ 6LUT-only slot policy
before producing a compact EmuFlow placement certificate.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping as MappingABC
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ImportError, ValidationError
from .io import file_sha256, read_json, write_json
from .native_tools import resolve_native_executable
from .xilinx_netlist import _IndexedCells
from .openparf import run_openparf, validate_openparf_runtime
from .openparf_native_driver import (
    native_metrics_path,
    validate_openparf_native_metrics,
)
from .xilinx_native_device_constraints import (
    load_xilinx_native_device_constraints,
    require_xilinx_native_constraint_capability,
)
from .xilinx_packing import (
    CONSTANT_TYPES,
    DUAL_OUTPUT_LUT_TYPE,
    FF_TYPES,
    LUT_TYPES,
    PACKED_SITE_NETLIST_SCHEMA,
    _derive_cascade_chains,
)
from .xilinx_physical_macros import build_xilinx_physical_macro_contract
from .xilinx_openparf_source_storage import (
    OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA,
    materialize_xilinx_openparf_atomic_source,
    read_xilinx_openparf_atomic_source,
    write_xilinx_openparf_atomic_source_storage,
)
from .xilinx_openparf_name_map_storage import (
    OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
    OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA,
    XilinxOpenparfAtomicNameMapView,
    read_xilinx_openparf_atomic_name_map,
    materialize_xilinx_openparf_atomic_name_map,
    write_xilinx_openparf_atomic_name_map_storage,
)


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
_SUPPORTED = (
    _SLICE_LUT_TYPES | FF_TYPES | {"CARRY8"}
    | set(_HARD_RESOURCES) | set(_MUX_RESOURCES)
)
_ALLOWED_CLUSTER_KEYS = {
    "id", "kind", "site_templates", "site_mode", "control_set",
    "control_sets", "assignments",
}
_ALLOWED_ASSIGNMENT_KEYS = {
    "instance", "cell_type", "bel", "bel_candidates",
}
_FF_CLOCK = "C"
_FF_ENABLE = "CE"
_FF_SR = {"FDCE": "CLR", "FDRE": "R", "FDPE": "PRE", "FDSE": "S"}
OPENPARF_FF_CONTROL_LEGALIZATION_SCHEMA = (
    "emuflow.openparf-ff-control-legalization/v1"
)
OPENPARF_RESULT_CHECKPOINT_SCHEMA = "emuflow.openparf-result-checkpoint/v1"
_MIN_ROUTABILITY_TARGET_DENSITY = 0.40
_MAX_ROUTABILITY_TARGET_DENSITY = 0.75
_TARGET_DENSITY_HEADROOM = 0.025
_QUALIFIED_LUTS_PER_SLICE = 8
_GLOBAL_PLACEMENT_STOP_OVERFLOW = 0.10
_DIAGNOSTIC_HANDOFF_OVERFLOW = 0.65
_DIAGNOSTIC_HANDOFF_PATIENCE = 64
# A diagnostic candidate must first see the same RUDY/pin-density feedback
# that the diagnostic handoff is intended to qualify.  The exact legalizer is
# the authority for physical occupancy, so the representative path hands off
# before the analytical density multiplier destroys HPWL.  NVDLA reaches its
# useful 0.40-density window near overflow 0.65; requiring 0.25 drove HPWL from
# 6.3M beyond 36M without reaching DirectLG.
_ROUTABILITY_ADJUSTMENT_OVERFLOW_THRESHOLD = _DIAGNOSTIC_HANDOFF_OVERFLOW
_ROUTABILITY_ADJUSTMENT_ITERATIONS = 6
_ROUTE_AREA_ADJUSTMENT_EXPONENT = 2.0
_MAX_ROUTE_AREA_ADJUSTMENT_RATE = 2.0
_MAX_PIN_AREA_ADJUSTMENT_RATE = 1.6
_HIGH_FANOUT_WEIGHT_THRESHOLD = 64
_HIGH_FANOUT_WEIGHT_EXPONENT = 0.5
_HIGH_FANOUT_WEIGHT_MAXIMUM = 4.0
_SLL_ADAM_STEP_SIZE = 0.002
_SLL_DELTA_EMA_WEIGHT = 0.9
_POST_ADJUSTMENT_ITERATION_RESERVE = 384
_POST_ADJUSTMENT_FEEDBACK_SAMPLES = 8
_LOGIC_FILLER_LIMIT = 65_536
_CLOCK_REGION_NAME = re.compile(r"^X([0-9]+)Y([0-9]+)$")
_MAX_CLOCKS_PER_REGION = 24
_DEVICE_STATIC_CACHE_LIMIT = 4
_DEVICE_STATIC_CACHE_LOCK = threading.RLock()
_DEVICE_STATIC_CACHE: "OrderedDict[Tuple[Any, ...], Dict[str, Any]]" = (
    OrderedDict()
)
_NATIVE_CARRY_Y_AXIS_ORDER_CACHE: Dict[Tuple[Any, str], str] = {}


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


def _slice_role_slot(cell_type: str, bel: str) -> int:
    """Map one source-sealed UltraScale+ slice BEL role to OpenPARF z."""

    if cell_type in _SLICE_LUT_TYPES:
        if bel not in {f"{letter}6LUT" for letter in "ABCDEFGH"}:
            raise ValidationError(f"unsupported LUT macro BEL role {bel!r}")
        return 2 * "ABCDEFGH".index(bel[0]) + 1
    if cell_type == "CARRY8":
        if bel != "CARRY8":
            raise ValidationError(f"unsupported CARRY8 macro BEL role {bel!r}")
        return 0
    roles = _MUX_BELS.get(cell_type)
    if roles is None or bel not in roles:
        raise ValidationError(
            f"unsupported {cell_type} macro BEL role {bel!r}"
        )
    return roles.index(bel)


def _bram_claims(tile: str, role: str) -> List[str]:
    if role == "lower":
        return [f"bram:{tile}:lower"]
    if role == "upper":
        return [f"bram:{tile}:upper"]
    if role == "whole":
        return [f"bram:{tile}:lower", f"bram:{tile}:upper"]
    raise ValidationError(f"unknown BRAM tile role {role!r}")


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _openparf_input_fingerprints(output_dir: Path) -> Dict[str, str]:
    """Seal every exported input that can affect one native placement."""

    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ValidationError("OpenPARF manifest file inventory is invalid")
    tracked = sorted(set(files) | {"manifest.json"})
    result: Dict[str, str] = {}
    for relative in tracked:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError("OpenPARF manifest contains an unsafe input path")
        path = output_dir / candidate
        if not path.is_file():
            raise ValidationError(f"OpenPARF input is missing: {relative}")
        if relative == "openparf.json":
            config = read_json(path)

            def relocate(value: Any) -> Any:
                if isinstance(value, Mapping):
                    return {
                        str(key): relocate(item) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, str):
                    candidate_path = Path(value)
                    if candidate_path.is_absolute():
                        try:
                            relative_path = candidate_path.relative_to(
                                output_dir.resolve()
                            )
                        except ValueError:
                            return value
                        return "$OPENPARF_OUTPUT/" + str(relative_path)
                return value

            serialized = json.dumps(
                relocate(config), sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            result[relative] = hashlib.sha256(serialized).hexdigest()
        else:
            result[relative] = _sha256(path)
    return result


def _reuse_openparf_result(output_dir: Path) -> Optional[Path]:
    """Return a prior result only when all input and output bytes still match."""

    checkpoint_path = output_dir / "openparf-result-checkpoint.json"
    if not checkpoint_path.is_file():
        return None
    try:
        checkpoint = read_json(checkpoint_path)
        if checkpoint.get("schema") != OPENPARF_RESULT_CHECKPOINT_SCHEMA:
            return None
        if checkpoint.get("input_sha256") != _openparf_input_fingerprints(output_dir):
            return None
        placement_relative = checkpoint.get("placement")
        metrics_relative = checkpoint.get("metrics")
        if not isinstance(placement_relative, str) or not isinstance(metrics_relative, str):
            return None
        placement_candidate = Path(placement_relative)
        metrics_candidate = Path(metrics_relative)
        if (
            placement_candidate.is_absolute()
            or metrics_candidate.is_absolute()
            or ".." in placement_candidate.parts
            or ".." in metrics_candidate.parts
        ):
            return None
        placement = output_dir / placement_candidate
        metrics = output_dir / metrics_candidate
        if not placement.is_file() or not metrics.is_file():
            return None
        if checkpoint.get("placement_sha256") != _sha256(placement):
            return None
        if checkpoint.get("metrics_sha256") != _sha256(metrics):
            return None
        validate_openparf_native_metrics(
            metrics, allow_detailed_legalization_handoff=True,
        )
        return placement.resolve()
    except (OSError, TypeError, ValueError, ValidationError):
        return None


def _write_openparf_result_checkpoint(
    output_dir: Path, placement: Path,
) -> Path:
    metrics = native_metrics_path(placement)
    if not placement.is_file() or not metrics.is_file():
        raise ValidationError("OpenPARF result checkpoint requires placement and metrics")
    try:
        placement_relative = placement.resolve().relative_to(output_dir.resolve())
        metrics_relative = metrics.resolve().relative_to(output_dir.resolve())
    except ValueError as error:
        raise ValidationError("OpenPARF result is outside its qualification root") from error
    checkpoint_path = output_dir / "openparf-result-checkpoint.json"
    write_json(checkpoint_path, {
        "schema": OPENPARF_RESULT_CHECKPOINT_SCHEMA,
        "input_sha256": _openparf_input_fingerprints(output_dir),
        "placement": str(placement_relative),
        "placement_sha256": _sha256(placement),
        "metrics": str(metrics_relative),
        "metrics_sha256": _sha256(metrics),
    }, compact=True)
    return checkpoint_path


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
    path: Path, descriptor: Any,
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
        with sqlite3.connect(
            f"file:{candidate.resolve()}?mode=ro&immutable=1", uri=True
        ) as database:
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

    def __init__(self, selector_path: Path) -> None:
        selector_path.parent.mkdir(parents=True, exist_ok=True)
        self.selector_path = selector_path
        self.lease = _PlacementCertificateLease(selector_path, exclusive=True)
        self.closed = False
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
                    with sqlite3.connect(
                        f"file:{self.previous_store}?mode=ro&immutable=1",
                        uri=True,
                    ) as previous_database:
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
        with self.temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        digest = _sha256(self.temporary_path)
        store_path = self.selector_path.parent / _certificate_store_name(
            self.selector_path, digest
        )
        if store_path.is_symlink():
            raise ValidationError(
                "OpenPARF atomic placement database is a symbolic link"
            )
        if store_path.exists():
            if _sha256(store_path) != digest:
                raise ValidationError(
                    "OpenPARF atomic placement content-addressed store disagrees"
                )
            self.temporary_path.unlink()
        else:
            os.replace(self.temporary_path, store_path)
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
            os.replace(temporary_selector, self.selector_path)
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
        self.lease.close()

    def __del__(self) -> None:
        self.close()


def write_xilinx_openparf_atomic_placement_certificate(
    path: Path, certificate: Mapping[str, Any]
) -> Dict[str, Any]:
    """Write a compact certificate whose sidecar owns assignment rows."""

    clusters = certificate.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError("OpenPARF atomic placement clusters are invalid")
    builder = _AtomicPlacementCertificateBuilder(path)
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
        self.closed = False
        try:
            if path.is_symlink():
                raise ValidationError(
                    "OpenPARF atomic placement certificate is a symbolic link"
                )
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
                    path, descriptor
                )
            assert isinstance(descriptor, Mapping)
            self.descriptor = descriptor
            database = sqlite3.connect(
                f"file:{database_path}?mode=ro&immutable=1", uri=True
            )
            self.database = database
            header = dict(selector)
            header.pop("cluster_storage", None)
            _validate_placement_database_contract(
                database, descriptor=descriptor, header=header,
                legacy=self.legacy,
            )
        except BaseException:
            self.close()
            raise

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


def _write_site_database(
    path: Path, coordinate_system: Mapping[str, Any]
) -> int:
    """Write the large physical site map once in a queryable representation."""

    path.unlink(missing_ok=True)
    sites = coordinate_system.get("sites")
    if not isinstance(sites, list) or not sites:
        raise ValidationError("OpenPARF atomic coordinate system is empty")
    # Populating several ordered B-trees directly on shared NFS turns roughly
    # two million small inserts into minutes of random writes.  Construct the
    # database in memory, create indexes after the bulk inserts, then emit its
    # pages once through SQLite's backup API.  The final bytes retain the same
    # read-only schema and remain under the required /research run directory.
    with sqlite3.connect(":memory:") as database:
        database.executescript("""
            PRAGMA page_size = 65536;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE sites (
                dense_x INTEGER NOT NULL,
                dense_y INTEGER NOT NULL,
                physical_x INTEGER NOT NULL,
                physical_y INTEGER NOT NULL,
                placement_x REAL NOT NULL,
                placement_y REAL NOT NULL,
                site TEXT NOT NULL,
                PRIMARY KEY (dense_x, dense_y)
            ) WITHOUT ROWID;
            CREATE TABLE resources (
                dense_x INTEGER NOT NULL,
                dense_y INTEGER NOT NULL,
                resource TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                PRIMARY KEY (dense_x, dense_y, resource)
            ) WITHOUT ROWID;
            CREATE TABLE physical_sites (
                resource TEXT NOT NULL,
                physical_site TEXT NOT NULL,
                dense_x INTEGER NOT NULL,
                dense_y INTEGER NOT NULL,
                slot INTEGER NOT NULL
            );
        """)
        database.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            ("schema", XILINX_OPENPARF_SITE_DATABASE_SCHEMA),
        )
        database.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            ("site_count", str(len(sites))),
        )
        database.executemany(
            "INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    int(item["dense_x"]), int(item["dense_y"]),
                    int(item["physical_x"]), int(item["physical_y"]),
                    float(item["placement_x"]), float(item["placement_y"]),
                    str(item["site"]),
                )
                for item in sites
            ),
        )
        database.executemany(
            "INSERT INTO resources VALUES (?, ?, ?, ?)",
            (
                (
                    int(item["dense_x"]), int(item["dense_y"]),
                    str(resource), int(capacity),
                )
                for item in sites
                for resource, capacity in item["resources"].items()
            ),
        )
        database.executemany(
            "INSERT INTO physical_sites VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(resource), str(site_name),
                    int(item["dense_x"]), int(item["dense_y"]), int(slot),
                )
                for item in sites
                for resource, site_names in item["physical_sites"].items()
                for slot, site_name in enumerate(site_names)
            ),
        )
        database.executescript("""
            CREATE UNIQUE INDEX physical_sites_identity
                ON physical_sites (resource, physical_site);
            CREATE INDEX physical_sites_coordinate
                ON physical_sites (dense_x, dense_y, resource, slot);
        """)
        database.commit()
        with sqlite3.connect(path) as destination:
            destination.executescript("""
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
            """)
            database.backup(destination, pages=4096)
    return len(sites)


def _materialize_site_database(
    path: Path, device_static: Dict[str, Any]
) -> int:
    """Write one immutable device site database, then hard-link later users."""

    with _DEVICE_STATIC_CACHE_LOCK:
        source = device_static.get("site_database")
        if isinstance(source, Path) and source.is_file():
            path.unlink(missing_ok=True)
            try:
                os.link(source, path)
                return int(device_static["site_count"])
            except OSError:
                # A different filesystem cannot share an inode. Preserve the
                # contract by materializing normally instead of copying a
                # mutable SQLite file behind the cache's back.
                pass
        count = _write_site_database(path, device_static["coordinate_system"])
        device_static["site_database"] = path
        device_static["site_count"] = count
        return count


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
    allow_legacy_name_map: bool = False,
) -> List[Dict[str, Any]]:
    """Read only requested site rows from the normalized placement contract."""

    name_map = (
        read_xilinx_openparf_atomic_name_map(
            name_map_path,
            mapped_sha256=mapped_sha256,
            packed_sha256=packed_sha256,
            architecture_sha256=architecture_sha256,
            allow_legacy=allow_legacy_name_map,
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
    uri = f"file:{database_path.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as database:
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
    allow_legacy_name_map: bool = False,
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
        allow_legacy_name_map=allow_legacy_name_map,
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


def _resource_sort_key(resource: str) -> Tuple[int, str]:
    if resource == "LUT":
        return (0, resource)
    if resource == "FF":
        return (1, resource)
    return (2, resource)


def _primitive_sort_key(primitive: str) -> Tuple[int, str]:
    if primitive in LUT_TYPES:
        return (0, primitive)
    if primitive in FF_TYPES:
        return (1, primitive)
    return (2, primitive)


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


def _physical_coordinate(site: Mapping[str, Any]) -> Tuple[int, int]:
    tile = site.get("tile")
    if isinstance(tile, Mapping):
        col, row = tile.get("grid_col"), tile.get("grid_row")
        if (
            isinstance(col, int) and not isinstance(col, bool) and col >= 0
            and isinstance(row, int) and not isinstance(row, bool) and row >= 0
        ):
            return col, row
    return int(site["x"]), int(site["y"])


def _native_carry_y_axis_order(
    architecture: ArchitectureDB,
    native: Optional[Mapping[str, Any]],
) -> str:
    """Orient dense Y so increasing coordinates follow native CARRY_NEXT.

    RapidWright tile rows increase from top to bottom on UltraScale+, while
    CARRY8 site Y and the directed CARRY_NEXT graph increase from bottom to
    top. OpenPARF's chain legalizer always lays source-to-sink instances at
    increasing dense Y. Derive the dense-axis orientation from the certified
    native graph instead of assuming tile-row and cascade directions agree.
    """

    if native is None:
        return "ascending"
    payload_sha256 = native.get("payload_sha256")
    cache_key = (
        (architecture, payload_sha256)
        if isinstance(payload_sha256, str) and payload_sha256
        else None
    )
    if cache_key is not None:
        with _DEVICE_STATIC_CACHE_LOCK:
            cached = _NATIVE_CARRY_Y_AXIS_ORDER_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw_sites = architecture.value.get("sites")
    if not isinstance(raw_sites, list):
        raise ValidationError("ArchitectureDB sites are invalid")
    sites_by_name = {
        site.get("name"): site
        for site in raw_sites
        if isinstance(site, Mapping) and isinstance(site.get("name"), str)
    }
    if len(sites_by_name) != len(raw_sites):
        raise ValidationError("ArchitectureDB site names are incomplete or duplicated")
    directions = set()
    checked_edges = 0
    for family in native.get("payload", {}).get("dedicated_adjacency", []):
        if not isinstance(family, Mapping) or family.get("kind") != "CARRY_NEXT":
            continue
        for chain in family.get("chains", []):
            if not isinstance(chain, list):
                raise ValidationError("native CARRY_NEXT chain is invalid")
            for source_name, target_name in zip(chain, chain[1:]):
                source = sites_by_name.get(source_name)
                target = sites_by_name.get(target_name)
                if source is None or target is None:
                    raise ValidationError(
                        "native CARRY_NEXT references an unknown architecture site"
                    )
                source_x, source_y = _physical_coordinate(source)
                target_x, target_y = _physical_coordinate(target)
                if source_x != target_x or source_y == target_y:
                    raise ValidationError(
                        "native CARRY_NEXT is not a directed vertical tile edge"
                    )
                directions.add("ascending" if target_y > source_y else "descending")
                checked_edges += 1
    if checked_edges == 0:
        return "ascending"
    if len(directions) != 1:
        raise ValidationError(
            "native CARRY_NEXT edges disagree on physical Y orientation"
        )
    result = directions.pop()
    if cache_key is not None:
        with _DEVICE_STATIC_CACHE_LOCK:
            _NATIVE_CARRY_Y_AXIS_ORDER_CACHE[cache_key] = result
    return result


def _pin_bit(cell: Mapping[str, Any], port: str) -> Any:
    connections = cell.get("connections")
    values = connections.get(port, []) if isinstance(connections, Mapping) else []
    if not isinstance(values, list) or len(values) > 1:
        raise ValidationError(f"cell port {port!r} is not a scalar pin")
    return values[0] if values else None


def _expanded_pins(cell: Mapping[str, Any]) -> List[Tuple[str, str, Any]]:
    directions = cell.get("port_directions")
    connections = cell.get("connections")
    if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
        raise ValidationError("mapped cell lacks explicit port metadata")
    if set(directions) != set(connections):
        raise ValidationError("mapped cell port metadata is incomplete")
    result = []
    for port, direction in sorted(directions.items()):
        if direction not in {"input", "output"}:
            raise ValidationError(f"mapped cell has unsupported {direction!r} pin")
        bits = connections[port]
        if not isinstance(bits, list):
            raise ValidationError(f"cell port {port!r} connections are invalid")
        for index, bit in enumerate(bits):
            pin = str(port) if len(bits) == 1 else f"{port}[{index}]"
            result.append((pin, str(direction), bit))
    return result


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


def _placement_sites(
    architecture: ArchitectureDB,
    hard_resources: Sequence[str],
    mux_resources: Sequence[str] = (),
    *, require_carry8: bool = False,
) -> List[Tuple[Dict[str, Any], Dict[str, int]]]:
    # ArchitectureDB.sites deliberately materializes every site's complete BEL
    # list.  A full XCVU19P contains more than 500k sites but only a handful of
    # distinct site templates, so doing that here multiplied a small template
    # database into a multi-GiB Python object several times.  Placement geometry
    # only needs the raw site coordinates; validate compatibility once per
    # template and keep inline-BEL sites as the uncommon per-site case.
    raw_sites = architecture.value["sites"]
    templates = architecture.value.get("site_templates", {})
    template_bels: Dict[str, List[Mapping[str, Any]]] = {}
    template_profiles: Dict[str, Dict[str, Any]] = {}

    def site_bels(site: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        inline = site.get("bels")
        if isinstance(inline, list):
            return inline
        template_name = str(site["template"])
        cached = template_bels.get(template_name)
        if cached is not None:
            return cached
        template = templates[template_name]
        modes = [template_name, *template.get("alternative_templates", [])]
        cached = [
            bel
            for mode in modes
            for bel in templates[mode]["bels"]
        ]
        template_bels[template_name] = cached
        return cached

    def site_profile(site: Mapping[str, Any]) -> Dict[str, Any]:
        template_name = site.get("template") if "bels" not in site else None
        if isinstance(template_name, str):
            cached = template_profiles.get(template_name)
            if cached is not None:
                return cached
        bels = site_bels(site)
        profile: Dict[str, Any] = {
            "lut_bels": {
                bel["name"] for bel in bels
                if any(
                    cell_type in LUT_TYPES
                    for cell_type in bel["compatible_cells"]
                )
                and bel["name"] in {f"{letter}6LUT" for letter in "ABCDEFGH"}
            },
            "ff_bels": {
                bel["name"] for bel in bels
                if any(
                    cell_type in FF_TYPES
                    for cell_type in bel["compatible_cells"]
                )
                and bel["name"] in {
                    name for letter in "ABCDEFGH"
                    for name in (f"{letter}FF", f"{letter}FF2")
                }
            },
            "carry_bels": sum(
                bel.get("name") == "CARRY8"
                and "CARRY8" in bel.get("compatible_cells", [])
                for bel in bels
            ),
            "dual_lut_bels": {
                bel["name"] for bel in bels
                if DUAL_OUTPUT_LUT_TYPE in bel.get("compatible_cells", [])
                and bel["name"] in {f"{letter}6LUT" for letter in "ABCDEFGH"}
            },
            "mux_bels": {
                primitive: {
                    bel["name"] for bel in bels
                    if primitive in bel["compatible_cells"]
                }
                for primitive in mux_resources
            },
            "hard_counts": {
                primitive: sum(
                    primitive in bel["compatible_cells"] for bel in bels
                )
                for primitive in hard_resources
            },
        }
        if isinstance(template_name, str):
            template_profiles[template_name] = profile
        return profile

    slice_sites: List[Dict[str, Any]] = []
    hard_sites: List[Tuple[Dict[str, Any], Dict[str, int]]] = []
    selected_sites: List[Dict[str, Any]] = []
    hard_capacity = Counter()
    validated_slice_profiles = set()
    for raw_site in raw_sites:
        profile = site_profile(raw_site)
        is_slice = str(raw_site.get("type", "")).upper().startswith("SLICE")
        if is_slice:
            site = dict(raw_site)
            slice_sites.append(site)
            selected_sites.append(site)
            profile_key = (
                "template", raw_site.get("template")
            ) if "bels" not in raw_site else ("inline", site["name"])
            if profile_key not in validated_slice_profiles:
                if len(profile["lut_bels"]) != 8 or len(profile["ff_bels"]) != 16:
                    raise ValidationError(
                        f"slice site {site['name']!r} does not expose the required "
                        "8 6LUT and 16 FF BELs"
                    )
                if require_carry8 and (
                    profile["carry_bels"] != 1
                    or len(profile["dual_lut_bels"]) != 8
                ):
                    raise ValidationError(
                        f"slice site {site['name']!r} lacks the complete "
                        "CARRY8/LUT6_2 physical model"
                    )
                for primitive in mux_resources:
                    expected_bels = set(_MUX_BELS[primitive])
                    if profile["mux_bels"][primitive] != expected_bels:
                        raise ValidationError(
                            f"slice site {site['name']!r} does not expose the exact "
                            f"{primitive} BEL set {sorted(expected_bels)!r}"
                        )
                validated_slice_profiles.add(profile_key)
            continue

        resources: Dict[str, int] = {}
        for primitive in hard_resources:
            compatible_bels = profile["hard_counts"][primitive]
            expected = 2 if primitive == "RAMB18E2" else 1
            if compatible_bels == expected:
                resources[_HARD_RESOURCES[primitive]] = expected
        if len(resources) > 1 and set(resources) != {"RAMB18E2", "RAMB36E2"}:
            raise ValidationError(
                f"site {raw_site['name']!r} is ambiguous for demanded hard resources"
            )
        if resources:
            site = dict(raw_site)
            selected_sites.append(site)
            hard_sites.append((site, resources))
            for primitive in hard_resources:
                resource = _HARD_RESOURCES[primitive]
                if resource in resources:
                    hard_capacity[primitive] += resources[resource]

    if not slice_sites:
        raise ValidationError("ArchitectureDB has no slice sites")
    # A real UltraScale+ tile may contain several sites of the same hard
    # resource (two DSP48E2s, four URAM288s, ...).  Bookshelf represents that
    # as one tile with resource capacity greater than one.  Multiple logic
    # sites at one coordinate are still unsupported because LUT/FF z encodes
    # BEL occupancy rather than a site selector.
    by_coordinate: Dict[Tuple[int, int], List[Mapping[str, Any]]] = defaultdict(list)
    for raw_site in selected_sites:
        by_coordinate[_physical_coordinate(raw_site)].append(raw_site)
    collisions = {
        coordinate: [site["name"] for site in sites]
        for coordinate, sites in by_coordinate.items()
        if sum(
            str(site.get("type", "")).upper().startswith("SLICE")
            for site in sites
        ) > 1
    }
    if collisions:
        raise ValidationError(
            "ArchitectureDB has coincident physical resources that the atomic "
            f"Bookshelf grid cannot distinguish: {collisions!r}"
        )
    result = [(
        site,
        {
            "LUT": 16,
            "FF": 16,
            **{
                primitive: len(_MUX_BELS[primitive])
                for primitive in mux_resources
            },
            **({"CARRY8": 1} if require_carry8 else {}),
        },
    ) for site in slice_sites]
    result.extend(hard_sites)
    missing = sorted(set(hard_resources) - set(hard_capacity))
    if missing:
        raise ValidationError(
            f"ArchitectureDB has no unique sites for hard resources {missing!r}"
        )
    return result


def _validate_native_placement_region(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
) -> Dict[str, Any]:
    """Reject logic regions that cannot support OpenPARF's 2-D density model.

    The Bookshelf adapter intentionally compresses physical tile coordinates,
    but it must not fold a one-dimensional or disconnected crop into a fake
    rectangular device.  Such a crop gives the nonlinear global placer a
    degenerate density domain and can turn finite net coordinates into NaN or
    infinite wirelength before direct legalization.
    """

    coordinates = [
        _physical_coordinate(site)
        for site, resources in sites
        if "LUT" in resources and "FF" in resources
    ]
    if not coordinates:
        raise ValidationError("OpenPARF atomic placement has no logic sites")
    x_axis = sorted({coordinate[0] for coordinate in coordinates})
    y_axis = sorted({coordinate[1] for coordinate in coordinates})
    if len(x_axis) < 2 or len(y_axis) < 2:
        raise ValidationError(
            "OpenPARF atomic placement requires a non-degenerate two-dimensional "
            "logic-site region; provide a crop spanning at least two physical "
            "rows and two physical columns"
        )
    x_index = {value: index for index, value in enumerate(x_axis)}
    y_index = {value: index for index, value in enumerate(y_axis)}
    occupied = {
        (x_index[coordinate[0]], y_index[coordinate[1]])
        for coordinate in coordinates
    }
    frontier = [next(iter(occupied))]
    reached = set(frontier)
    while frontier:
        x, y = frontier.pop()
        for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if neighbor in occupied and neighbor not in reached:
                reached.add(neighbor)
                frontier.append(neighbor)
    if reached != occupied:
        raise ValidationError(
            "OpenPARF atomic placement requires one connected logic-site region; "
            "the supplied ArchitectureDB crop has disconnected islands"
        )
    return {
        "logic_sites": len(occupied),
        "dense_width": len(x_axis),
        "dense_height": len(y_axis),
        "occupied_fraction": len(occupied) / (len(x_axis) * len(y_axis)),
    }


def _clock_region_site_key(
    site: Mapping[str, Any],
) -> Optional[Tuple[str, str, str]]:
    region = site.get("physical_region")
    if not isinstance(region, Mapping):
        return None
    slr = region.get("slr")
    clock_region = region.get("clock_region")
    site_type = site.get("type")
    if not all(
        isinstance(value, str) and value
        for value in (slr, clock_region, site_type)
    ):
        raise ValidationError("ArchitectureDB physical-region metadata is invalid")
    return slr, clock_region, site_type


def _expose_all_clock_region_sites(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
) -> Tuple[
    List[Tuple[Mapping[str, Any], Mapping[str, int]]], Dict[str, Any]
]:
    """Expose every qualified physical site to placement and legalization.

    Analytical target density already reserves whitespace. Removing another
    fraction of the sites would apply the same headroom twice and distort its
    spatial distribution before the density optimizer can choose where that
    whitespace is useful.
    """

    groups: Counter = Counter()
    for item in sites:
        key = _clock_region_site_key(item[0])
        if key is not None:
            groups[key] += 1
    if not groups:
        return list(sites), {
            "status": "not-applicable",
            "policy": "full-qualified-site-domain",
            "limit": None,
            "groups": 0, "original_sites": len(sites),
            "available_sites": len(sites), "reserved_sites": 0,
        }

    return list(sites), {
        "status": "pass",
        "policy": "full-qualified-site-domain",
        "limit": 1.0,
        "groups": len(groups),
        "original_sites": len(sites),
        "available_sites": len(sites),
        "reserved_sites": 0,
        "minimum_group_available_sites": min(groups.values()),
        "maximum_group_available_sites": max(groups.values()),
    }


def _validate_native_density_contract(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
    atoms: Sequence[Mapping[str, str]],
) -> Tuple[Dict[str, int], Dict[str, Dict[str, float]], float]:
    """Normalize density to qualified BELs, independently of slot encoding."""

    demand = Counter(atom["resource"] for atom in atoms)

    def density_capacity(resources: Mapping[str, int], resource: str) -> int:
        capacity = resources.get(resource, 0)
        # DirectLG retains sixteen z positions for the 5LUT/6LUT BLE encoding,
        # but allow_paired_luts=0 makes only eight positions usable.  Its slot
        # namespace must not halve each LUT's analytical area or double the
        # physical headroom assumed by global placement.
        return (
            min(capacity, _QUALIFIED_LUTS_PER_SLICE)
            if resource == "LUT" else capacity
        )

    per_site_capacity = {
        resource: max(
            density_capacity(resources, resource) for _site, resources in sites
        )
        for resource in demand
    }
    areas: Dict[str, Tuple[float, float, int]] = {}
    for resource in sorted(demand, key=_resource_sort_key):
        unit_capacity = per_site_capacity[resource]
        if unit_capacity <= 0:
            raise ValidationError(
                f"OpenPARF atomic placement has no {resource} model capacity"
            )
        available_units = sum(
            density_capacity(resources, resource) for _site, resources in sites
        )
        movable_area = demand[resource] / unit_capacity
        placeable_area = available_units / unit_capacity
        filler_units = available_units - demand[resource]
        areas[resource] = (movable_area, placeable_area, filler_units)
    maximum_utilization = max(
        movable_area / placeable_area
        for movable_area, placeable_area, _filler_units in areas.values()
    )
    target_density = max(
        _MIN_ROUTABILITY_TARGET_DENSITY,
        min(
            _MAX_ROUTABILITY_TARGET_DENSITY,
            maximum_utilization + _TARGET_DENSITY_HEADROOM,
        ),
    )
    result: Dict[str, Dict[str, float]] = {}
    for resource in sorted(demand, key=_resource_sort_key):
        movable_area, placeable_area, filler_units = areas[resource]
        target_area = target_density * placeable_area
        if not (
            math.isfinite(movable_area)
            and math.isfinite(placeable_area)
            and 0.0 < movable_area < target_area
            and filler_units > 0
        ):
            raise ValidationError(
                "OpenPARF atomic placement density domain has no finite headroom "
                f"for {resource}: demand={demand[resource]}, "
                f"capacity={int(placeable_area * per_site_capacity[resource])}, "
                f"target_density={target_density}"
            )
        result[resource] = {
            "movable_area": movable_area,
            "placeable_area": placeable_area,
            "target_area": target_area,
            "filler_units": filler_units,
        }
    return per_site_capacity, result, target_density


class _IndexedAtomSequence(Sequence[Mapping[str, str]]):
    """File-backed lexical atom relation used only during Bookshelf export."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="emuflow-openparf-atoms-"
        )
        self.path = Path(self._temporary.name) / "atoms.sqlite"
        self._writer = sqlite3.connect(self.path)
        self._writer.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE raw_atoms(
              instance TEXT PRIMARY KEY,
              cell_type TEXT NOT NULL,
              source_cluster TEXT NOT NULL,
              resource TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        self._count: Optional[int] = None
        self._closed = False

    def append(self, atom: Mapping[str, str]) -> None:
        if self._count is not None or self._closed:
            raise ValidationError("OpenPARF atom index is not writable")
        try:
            self._writer.execute(
                "INSERT INTO raw_atoms VALUES (?,?,?,?)",
                (
                    atom["instance"], atom["cell_type"],
                    atom["source_cluster"], atom["resource"],
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                f"mapped instance {atom.get('instance')!r} is packed twice"
            ) from error

    def finalize(self) -> None:
        if self._count is not None:
            return
        if self._closed:
            raise ValidationError("OpenPARF atom index is closed")
        self._writer.executescript(
            """
            CREATE TABLE atoms(
              seq INTEGER PRIMARY KEY,
              instance TEXT NOT NULL UNIQUE,
              cell_type TEXT NOT NULL,
              source_cluster TEXT NOT NULL,
              resource TEXT NOT NULL
            );
            INSERT INTO atoms(instance,cell_type,source_cluster,resource)
            SELECT instance,cell_type,source_cluster,resource
            FROM raw_atoms ORDER BY instance;
            DROP TABLE raw_atoms;
            CREATE INDEX atoms_cell_type ON atoms(cell_type);
            CREATE INDEX atoms_resource ON atoms(resource);
            """
        )
        self._count = int(
            self._writer.execute("SELECT count(*) FROM atoms").fetchone()[0]
        )
        self._writer.commit()
        self._writer.close()

    def _connect(self) -> sqlite3.Connection:
        if self._count is None or self._closed:
            raise ValidationError("OpenPARF atom index is not readable")
        connection = sqlite3.connect(
            f"file:{self.path.resolve()}?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> Dict[str, str]:
        return {
            "instance": str(row["instance"]),
            "cell_type": str(row["cell_type"]),
            "source_cluster": str(row["source_cluster"]),
            "resource": str(row["resource"]),
        }

    def __len__(self) -> int:
        if self._count is None or self._closed:
            raise ValidationError("OpenPARF atom index is not readable")
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, str]]:
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT instance,cell_type,source_cluster,resource "
                "FROM atoms ORDER BY seq"
            ):
                yield self._record(row)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        count = len(self)
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError(index)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT instance,cell_type,source_cluster,resource "
                "FROM atoms WHERE seq=?", (index + 1,),
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return self._record(row)

    def ordinal_for_instance(self, instance: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT seq FROM atoms WHERE instance=?", (instance,)
            ).fetchone()
        if row is None:
            raise KeyError(instance)
        return int(row[0]) - 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._count is None:
            self._writer.close()
        self._temporary.cleanup()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _collect_atoms(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str],
    *, allow_hardblock_cascades: bool = False,
    physical_macro_contract: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, Mapping[str, Any], _IndexedAtomSequence]:
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    cascades = packed.get("cascade_chains", [])
    if not isinstance(cascades, Sequence) or isinstance(cascades, (str, bytes)):
        raise ValidationError("atomic mixed-resource cascade constraints are invalid")
    if cascades and not allow_hardblock_cascades:
        raise ValidationError("atomic mixed-resource adapter rejects all cascade constraints")
    if allow_hardblock_cascades:
        for chain in cascades:
            if (
                not isinstance(chain, Mapping)
                or chain.get("cell_type") not in set(_HARD_RESOURCES) | {"CARRY8"}
                or not isinstance(chain.get("instances"), list)
                or len(chain["instances"]) < 2
            ):
                raise ValidationError(
                    "typed hardblock route accepts only supported hardblock "
                    "cascade chains"
                )
            if chain.get("cell_type") == "RAMB18E2":
                raise ValidationError(
                    "RAMB18E2 dedicated cascades are not yet qualified"
                )
    atoms = _IndexedAtomSequence()
    site_macros = []
    if physical_macro_contract is not None:
        site_macros = [
            macro for macro in physical_macro_contract.get("site_macros", [])
            if isinstance(macro, Mapping)
            and macro.get("kind") in {
                "muxf7-cone", "muxf8-cone", "muxf9-cone", "carry8-lut6_2",
                *(
                    {"ramb18-half-site-occupancy"}
                    if packed.get("schema") == OPENPARF_ATOMIC_SOURCE_SCHEMA
                    else set()
                ),
            }
        ]
    site_macro_by_members = {
        frozenset(
            member["instance"] for member in macro.get("members", [])
            if isinstance(member, Mapping)
        ): macro
        for macro in site_macros
    }
    if len(site_macro_by_members) != len(site_macros):
        raise ValidationError("physical slice macro ownership is ambiguous")
    covered_site_macros = set()
    clusters = packed.get("clusters")
    if not isinstance(clusters, Sequence) or not clusters:
        raise ValidationError("PackedSiteNetlist clusters are invalid")
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            raise ValidationError("PackedSiteNetlist cluster is invalid")
        extra = set(cluster) - _ALLOWED_CLUSTER_KEYS
        if extra:
            raise ValidationError(
                f"cluster {cluster.get('id')!r} has unsupported relative/physical "
                f"constraints {sorted(extra)!r}"
            )
        kind = cluster.get("kind")
        if kind not in {"slice", "carry", "hard"}:
            raise ValidationError(
                f"atomic mixed-resource adapter rejects cluster kind {kind!r}"
            )
        assignments = cluster.get("assignments")
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError(f"cluster {cluster.get('id')!r} has no assignments")
        constrained_assignments = [
            assignment for assignment in assignments
            if isinstance(assignment, Mapping)
            and assignment.get("cell_type") in set(_MUX_RESOURCES) | {
                "CARRY8", DUAL_OUTPUT_LUT_TYPE,
            }
        ]
        if constrained_assignments:
            expected_kind = "carry" if any(
                assignment.get("cell_type") == "CARRY8"
                for assignment in constrained_assignments
            ) else "slice"
            if kind != expected_kind:
                raise ValidationError(
                    "physical slice macro has an invalid cluster kind"
                )
            macro = site_macro_by_members.get(frozenset(
                assignment.get("instance") for assignment in assignments
                if isinstance(assignment, Mapping)
            ))
            if macro is None:
                raise ValidationError(
                    "slice cluster does not match one complete connectivity-derived macro"
                )
            expected_roles = {
                member["instance"]: member["physical_role"]
                for member in macro["members"]
            }
            actual_roles = {
                assignment.get("instance"): assignment.get("bel")
                for assignment in assignments
            }
            if actual_roles != expected_roles:
                raise ValidationError(
                    "slice cluster differs from the connectivity-derived BEL roles"
                )
            covered_site_macros.add(macro["id"])
        hard_types = {
            assignment.get("cell_type")
            for assignment in assignments if isinstance(assignment, Mapping)
        }
        if kind == "hard" and hard_types == {"RAMB18E2"}:
            if packed.get("schema") == OPENPARF_ATOMIC_SOURCE_SCHEMA:
                macro = site_macro_by_members.get(frozenset(
                    assignment.get("instance") for assignment in assignments
                    if isinstance(assignment, Mapping)
                ))
                if (
                    len(assignments) != 1
                    or cluster.get("site_mode") is not None
                    or macro is None
                    or macro.get("kind") != "ramb18-half-site-occupancy"
                    or assignments[0].get("bel") is not None
                    or assignments[0].get("bel_candidates")
                    != ["RAMB18E2_L", "RAMB18E2_U"]
                ):
                    raise ValidationError(
                        "RAMB18E2 atomic half-site occupancy is invalid"
                    )
                covered_site_macros.add(macro["id"])
            else:
                bels = [assignment.get("bel") for assignment in assignments]
                if (
                    len(assignments) > 2
                    or len(bels) != len(set(bels))
                    or set(bels) - {"RAMB18E2_L", "RAMB18E2_U"}
                    or cluster.get("site_mode") != f"RAMB18E2x{len(assignments)}"
                ):
                    raise ValidationError("RAMB18E2 source packing is invalid")
        elif kind == "hard" and (
            len(assignments) != 1
            or len(hard_types) != 1
            or next(iter(hard_types), None) not in _HARD_RESOURCES
            or cluster.get("site_mode") is not None
        ):
            raise ValidationError(
                "hard-resource support requires one DSP48E2/RAMB36E2/URAM288 "
                "or one/two source-packed RAMB18E2 cells"
            )
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                raise ValidationError("packed assignment is invalid")
            extra_assignment = set(assignment) - _ALLOWED_ASSIGNMENT_KEYS
            if extra_assignment:
                raise ValidationError(
                    f"packed assignment has unsupported relative constraints "
                    f"{sorted(extra_assignment)!r}"
                )
            instance = assignment.get("instance")
            cell_type = assignment.get("cell_type")
            if (
                not isinstance(instance, str) or not instance
                or not isinstance(cell_type, str) or not cell_type
            ):
                raise ValidationError("packed assignment does not match mapped JSON")
            if cell_type not in _SUPPORTED:
                raise ValidationError(
                    f"atomic mixed-resource adapter rejects primitive {cell_type!r}"
                )
            if kind == "slice" and cell_type not in LUT_TYPES | FF_TYPES | set(_MUX_RESOURCES):
                raise ValidationError(
                    f"slice cluster contains hard primitive {cell_type!r}"
                )
            if kind == "carry" and cell_type not in {
                "CARRY8", DUAL_OUTPUT_LUT_TYPE,
            }:
                raise ValidationError(
                    f"carry cluster contains unsupported primitive {cell_type!r}"
                )
            if kind == "hard" and cell_type not in _HARD_RESOURCES:
                raise ValidationError("hard cluster contains an unsupported primitive")
            atoms.append({
                "instance": instance,
                "cell_type": cell_type,
                "source_cluster": str(cluster.get("id")),
                "resource": (
                    "FF" if cell_type in FF_TYPES
                    else "LUT" if cell_type in _SLICE_LUT_TYPES
                    else "CARRY8" if cell_type == "CARRY8"
                    else _MUX_RESOURCES[cell_type] if cell_type in _MUX_RESOURCES
                    else _HARD_RESOURCES[cell_type]
                ),
            })
    expected_site_macros = {macro["id"] for macro in site_macros}
    if covered_site_macros != expected_site_macros:
        raise ValidationError("connectivity-derived slice macro coverage is incomplete")
    # Validate type and complete ownership with one merge over the authoritative
    # mapped index. Atom identities are sorted in a private file-backed
    # relation, so neither the final atom population nor a duplicate ownership
    # set remains resident in Python.
    atoms.finalize()
    atom_iterator = iter(atoms)
    current = next(atom_iterator, None)
    missing: List[str] = []
    for name, cell in _ordered_cell_items(cells):
        if not isinstance(cell, Mapping) or not isinstance(cell.get("type"), str):
            raise ValidationError("mapped JSON cell is invalid")
        mapped_cell_type = cell["type"]
        if mapped_cell_type in {"GND", "VCC"}:
            continue
        if current is not None and current["instance"] < name:
            raise ValidationError("packed assignment does not match mapped JSON")
        if current is None or current["instance"] > name:
            if len(missing) < 16:
                missing.append(name)
            continue
        if current["cell_type"] != mapped_cell_type:
            raise ValidationError("packed assignment does not match mapped JSON")
        current = next(atom_iterator, None)
    if current is not None:
        raise ValidationError("packed assignment does not match mapped JSON")
    if missing:
        raise ValidationError(
            "atomic mixed-resource adapter requires complete physical cell coverage; "
            f"uncovered cells: {missing!r}"
        )
    counts = Counter(atom["cell_type"] in FF_TYPES for atom in atoms)
    if not counts[False] or not counts[True]:
        raise ValidationError(
            "pinned OpenPARF direct legalization requires at least one LUT and one FF"
        )
    return selected_top, cells, atoms


def _ordered_cell_items(
    cells: Mapping[str, Any],
) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    indexed = getattr(cells, "items_by_id", None)
    if callable(indexed):
        yield from indexed()
        return
    yield from sorted(cells.items())


class _OrdinalSafeNames(MappingABC[str, str]):
    """Resolve deterministic ``aN`` identities without an instance-sized dict.

    ``_collect_atoms`` has already established unique lexical instance order.
    The ordinal is therefore the safe name's only stored information. Rare
    macro lookups use binary search; sequential exporters use ``name_at`` and
    remain linear.
    """

    def __init__(self, atoms: Sequence[Mapping[str, str]]) -> None:
        self.atoms = atoms

    def __len__(self) -> int:
        return len(self.atoms)

    def __iter__(self) -> Iterator[str]:
        return (atom["instance"] for atom in self.atoms)

    def name_at(self, index: int, instance: str) -> str:
        if not 0 <= index < len(self.atoms):
            raise KeyError(instance)
        atom = self.atoms[index]
        if atom["instance"] != instance:
            raise KeyError(instance)
        return f"a{index}"

    def __getitem__(self, instance: str) -> str:
        if not isinstance(instance, str):
            raise KeyError(instance)
        indexed_lookup = getattr(self.atoms, "ordinal_for_instance", None)
        if callable(indexed_lookup):
            return f"a{indexed_lookup(instance)}"
        lower = 0
        upper = len(self.atoms)
        while lower < upper:
            middle = (lower + upper) // 2
            candidate = self.atoms[middle]["instance"]
            if candidate < instance:
                lower = middle + 1
            else:
                upper = middle
        return self.name_at(lower, instance)


def build_xilinx_openparf_atomic_source(
    mapped_path: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
) -> Mapping[str, Any]:
    """Build an unplaced singleton-atom source without legacy site packing."""

    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping) or not cells:
        raise ValidationError("mapped JSON cells are invalid or empty")
    unsupported = set()
    for cell in cells.values():
        if not isinstance(cell, Mapping):
            unsupported.add(f"invalid-cell-record:{type(cell).__name__}")
            continue
        cell_type = cell.get("type")
        if cell_type not in _SUPPORTED | CONSTANT_TYPES:
            unsupported.add(cell_type)
    unsupported = sorted(unsupported, key=str)
    if unsupported:
        raise ValidationError(
            "OpenPARF atomic source does not support primitives: "
            + ", ".join(str(item) for item in unsupported)
        )
    cascades = _derive_cascade_chains(cells)
    unsupported_cascades = [
        chain for chain in cascades if chain.get("cell_type") != "CARRY8"
    ]
    if unsupported_cascades:
        raise ValidationError(
            "OpenPARF atomic source rejects non-CARRY8 dedicated cascade connectivity"
        )
    has_slice_macro = any(
        cell.get("type") in set(_MUX_RESOURCES) | {"CARRY8", DUAL_OUTPUT_LUT_TYPE}
        for cell in cells.values()
    )
    macro_contract = (
        build_xilinx_physical_macro_contract(
            mapped_path, top=selected_top, source=mapped
        )
        if has_slice_macro else {"site_macros": []}
    )
    unsupported_site_macros = [
        macro for macro in macro_contract.get("site_macros", [])
        if macro.get("kind") not in {
            "muxf7-cone", "muxf8-cone", "muxf9-cone", "carry8-lut6_2",
            "ramb18-half-site-occupancy",
        }
    ]
    if unsupported_site_macros:
        raise ValidationError(
            "OpenPARF atomic source does not yet support site macros: "
            + ", ".join(sorted(str(macro.get("kind")) for macro in unsupported_site_macros))
        )
    macro_clusters = []
    for macro in macro_contract.get("site_macros", []):
        assignments = []
        for member in macro["members"]:
            instance = member["instance"]
            if macro["kind"] == "ramb18-half-site-occupancy":
                assignments.append({
                    "instance": instance,
                    "cell_type": member["cell_type"],
                    "bel_candidates": list(member["physical_roles"]),
                })
            else:
                assignments.append({
                    "instance": instance,
                    "cell_type": member["cell_type"],
                    "bel": member["physical_role"],
                    "bel_candidates": [member["physical_role"]],
                })
        carry_macro = macro["kind"] == "carry8-lut6_2"
        ramb18_macro = macro["kind"] == "ramb18-half-site-occupancy"
        macro_clusters.append({
            "id": macro["id"],
            "kind": "carry" if carry_macro else "hard" if ramb18_macro else "slice",
            "site_templates": (
                ["RAMB18E2"] if ramb18_macro else ["SLICEL", "SLICEM"]
            ),
            "control_set": None,
            "assignments": assignments,
        })
    return write_xilinx_openparf_atomic_source_storage(
        output_path,
        mapped_path=mapped_path,
        mapped_value=mapped,
        mapped_sha256=mapped_sha256 or _sha256(mapped_path),
        top=selected_top,
        macro_clusters=macro_clusters,
        cascades=cascades,
        physical_macro_count=len(macro_contract.get("site_macros", [])),
    )


def probe_xilinx_openparf_atomic_eligibility(
    mapped: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture: ArchitectureDB,
    *,
    top: Optional[str] = None,
    allow_typed_hardblocks: bool = False,
) -> Dict[str, Any]:
    """Return a side-effect-free design-specific adapter decision."""

    atoms: Optional[_IndexedAtomSequence] = None
    try:
        _selected_top, _cells, atoms = _collect_atoms(mapped, packed, top)
        if (
            any(atom["cell_type"] == "RAMB18E2" for atom in atoms)
            and not allow_typed_hardblocks
        ):
            raise ValidationError(
                "RAMB18E2 requires the typed hardblock group legalizer"
            )
        hard = sorted({
            atom["cell_type"] for atom in atoms
            if atom["cell_type"] in _HARD_RESOURCES
        })
        sites = _placement_sites(architecture, hard)
        _validate_native_placement_region(sites)
        _validate_native_density_contract(sites, atoms)
        resources = Counter(atom["resource"] for atom in atoms)
        slice_count = sum("LUT" in capacity for _site, capacity in sites)
        if resources["LUT"] > 8 * slice_count or resources["FF"] > 16 * slice_count:
            raise ValidationError("atomic LUT/FF demand exceeds slice capacity")
        return {
            "status": "adapter_required", "eligible": True,
            "adapter_validation": "pass",
            "reason": (
                "ordinary LUT/FF clusters and independent singleton hard resources "
                "can be atomized without discarding a dedicated, relative, "
                "cascade, half-site, or coincident-site constraint"
            ),
            "atoms": len(atoms), "sites": len(sites),
            "resources": dict(sorted(resources.items())),
        }
    except ValidationError as error:
        return {
            "status": "adapter_required", "eligible": False,
            "adapter_validation": "missing", "reason": str(error),
        }
    finally:
        if atoms is not None:
            atoms.close()


def _render_library(
    cells: Mapping[str, Any],
    atoms: Sequence[Mapping[str, str]],
    *,
    platform_mux_types: Sequence[str] = (),
) -> str:
    ports_by_type: Dict[str, Dict[str, str]] = defaultdict(dict)
    for atom in atoms:
        cell_type = atom["cell_type"]
        cell = cells[atom["instance"]]
        for port, direction, _bit in _expanded_pins(cell):
            previous = ports_by_type[cell_type].setdefault(port, direction)
            if previous != direction:
                raise ValidationError(
                    f"primitive {cell_type!r} has inconsistent pin direction"
                )
    # The patched OpenPARF Bookshelf reader uses the complete F7/F8/F9 model
    # hierarchy to select its UltraScale grammar.  Declare unused upper mux
    # levels as zero-instance library models; they do not create nodes, nets,
    # fillers, or placement demand.
    for cell_type in platform_mux_types:
        ports_by_type.setdefault(cell_type, {}).update({
            "I0": "input", "I1": "input", "S": "input", "O": "output",
        })
    blocks = []
    for cell_type, ports in sorted(ports_by_type.items()):
        lines = [f"CELL {cell_type}"]
        for port, direction in sorted(ports.items()):
            suffix = ""
            if cell_type in FF_TYPES and port == _FF_CLOCK:
                suffix = " CLOCK"
            elif cell_type in FF_TYPES and port == _FF_ENABLE:
                suffix = " CTRL_CE"
            elif cell_type in FF_TYPES and port == _FF_SR[cell_type]:
                suffix = " CTRL_SR"
            elif cell_type == "CARRY8" and port in {"CI", "CO[7]"}:
                suffix = " CAS"
            lines.append(f"  PIN {port} {direction.upper()}{suffix}")
        lines.append("END CELL")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_nets(
    cells: Mapping[str, Any], atoms: Sequence[Mapping[str, str]], names: Mapping[str, str]
) -> Tuple[str, int, int]:
    endpoints: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for atom in atoms:
        instance = atom["instance"]
        cell = cells[instance]
        for port, direction, bit in _expanded_pins(cell):
            if isinstance(bit, int) and not isinstance(bit, bool):
                endpoints[bit].append(
                    (names[instance], port, direction)
                )
    lines = []
    emitted = 0
    dropped_single_endpoint = 0
    for _bit, pins in sorted(endpoints.items()):
        drivers = [pin for pin in pins if pin[2] == "output"]
        if len(drivers) > 1:
            raise ValidationError("mapped net has multiple atomic-resource drivers")
        if len(pins) < 2:
            dropped_single_endpoint += 1
            continue
        ordered = [*drivers, *(pin for pin in pins if pin[2] != "output")]
        lines.append(f"net n{emitted} {len(ordered)}")
        lines.extend(f"  {instance} {port}" for instance, port, _direction in ordered)
        lines.append("endnet")
        emitted += 1
    return "\n".join(lines) + "\n", emitted, dropped_single_endpoint


def _bookshelf_library(
    ports_by_type: Dict[str, Dict[str, str]],
    platform_mux_types: Sequence[str],
) -> str:
    for cell_type in platform_mux_types:
        ports_by_type.setdefault(cell_type, {}).update({
            "I0": "input", "I1": "input", "S": "input", "O": "output",
        })
    library_blocks = []
    for cell_type, ports in sorted(ports_by_type.items()):
        lines = [f"CELL {cell_type}"]
        for port, direction in sorted(ports.items()):
            suffix = ""
            if cell_type in FF_TYPES and port == _FF_CLOCK:
                suffix = " CLOCK"
            elif cell_type in FF_TYPES and port == _FF_ENABLE:
                suffix = " CTRL_CE"
            elif cell_type in FF_TYPES and port == _FF_SR[cell_type]:
                suffix = " CTRL_SR"
            elif cell_type == "CARRY8" and port in {"CI", "CO[7]"}:
                suffix = " CAS"
            lines.append(f"  PIN {port} {direction.upper()}{suffix}")
        lines.append("END CELL")
        library_blocks.append("\n".join(lines))
    library = "\n\n".join(library_blocks) + "\n"
    return library


def _render_indexed_bookshelf(
    cells: _IndexedCells,
    atoms: _IndexedAtomSequence,
    *,
    platform_mux_types: Sequence[str],
    nets_path: Optional[Path],
) -> Tuple[str, Optional[str], int, int]:
    """Keep typed pin traversal and net text emission entirely in C++."""

    executable = resolve_native_executable("emuflow_xilinx_bookshelf_exporter")
    database = next(
        row[2] for row in cells.connection.execute("PRAGMA database_list")
        if row[1] == "main"
    )
    parent = nets_path.parent if nets_path is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".emuflow-bookshelf-", dir=parent
    ) as temporary:
        root = Path(temporary)
        output = root / "design.nets"
        report = root / "summary.tsv"
        result = subprocess.run(
            [executable, Path(database).resolve().as_uri() + "?mode=ro&immutable=1",
             atoms.path.resolve().as_uri() + "?mode=ro&immutable=1",
             str(output), str(report), str(root / "endpoints.sqlite")],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise ValidationError(
                "native OpenPARF Bookshelf export failed: " + result.stderr.strip()
            )
        ports_by_type: Dict[str, Dict[str, str]] = defaultdict(dict)
        try:
            # The report contains primitive pin signatures and constant-size
            # counters only; no cell/net/endpoint populations cross into Python.
            with report.open(encoding="utf-8") as stream:
                header = stream.readline().rstrip("\n").split("\t")
                if len(header) != 4 or header[0] != "EMUFLOW_BOOKSHELF_V1":
                    raise ValueError("invalid native Bookshelf report header")
                count, emitted, dropped = map(int, header[1:])
                if count != len(atoms) or min(emitted, dropped) < 0:
                    raise ValueError("invalid native Bookshelf counts")
                for line in stream:
                    cell_type, port, direction = line.rstrip("\n").split("\t")
                    if direction not in {"0", "1"}:
                        raise ValueError("invalid native Bookshelf direction")
                    ports_by_type[cell_type][port] = (
                        "output" if direction == "1" else "input"
                    )
            library = _bookshelf_library(ports_by_type, platform_mux_types)
        except (OSError, ValueError) as error:
            raise ValidationError("native Bookshelf summary is invalid") from error
        if nets_path is None:
            return library, output.read_text(encoding="utf-8"), emitted, dropped
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(output, nets_path)
        return library, None, emitted, dropped


def _render_library_and_nets(
    cells: Mapping[str, Any],
    atoms: Sequence[Mapping[str, str]],
    names: Mapping[str, str],
    *,
    platform_mux_types: Sequence[str] = (),
    nets_path: Optional[Path] = None,
) -> Tuple[str, Optional[str], int, int]:
    """Render library data and stream net endpoints through a file relation.

    Atom and mapped-cell identities are merged in lexical order. Endpoint rows
    are spooled once, then read by numeric net ID and source order; only one
    net's fanout is resident while ``design.nets`` is emitted. An omitted
    ``nets_path`` explicitly materializes the final text for small compatibility
    callers and differential tests.
    """

    if (
        isinstance(cells, _IndexedCells)
        and isinstance(atoms, _IndexedAtomSequence)
        and isinstance(names, _OrdinalSafeNames)
    ):
        return _render_indexed_bookshelf(
            cells, atoms, platform_mux_types=platform_mux_types,
            nets_path=nets_path,
        )
    ports_by_type: Dict[str, Dict[str, str]] = defaultdict(dict)
    atom_iterator = iter(enumerate(atoms))
    current = next(atom_iterator, None)
    with tempfile.TemporaryDirectory(
        prefix="emuflow-openparf-net-endpoints-"
    ) as temporary:
        endpoint_path = Path(temporary) / "endpoints.sqlite"
        connection = sqlite3.connect(endpoint_path)
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE endpoints(
              net INTEGER NOT NULL,
              seq INTEGER PRIMARY KEY,
              instance TEXT NOT NULL,
              port TEXT NOT NULL,
              is_driver INTEGER NOT NULL CHECK(is_driver IN (0,1))
            );
            """
        )
        batch: List[Tuple[int, int, str, str, int]] = []
        endpoint_seq = 0
        try:
            for instance, cell in _ordered_cell_items(cells):
                if (
                    isinstance(cell, Mapping)
                    and cell.get("type") in {"GND", "VCC"}
                ):
                    continue
                if current is None or current[1]["instance"] != instance:
                    raise ValidationError(
                        "mapped JSON does not cover every OpenPARF atom"
                    )
                atom_index, atom = current
                if (
                    not isinstance(cell, Mapping)
                    or cell.get("type") != atom["cell_type"]
                ):
                    raise ValidationError(
                        "OpenPARF atom does not match mapped JSON"
                    )
                cell_type = atom["cell_type"]
                safe_name = (
                    f"a{atom_index}"
                    if isinstance(names, _OrdinalSafeNames)
                    else names[instance]
                )
                for port, direction, bit in _expanded_pins(cell):
                    previous = ports_by_type[cell_type].setdefault(
                        port, direction
                    )
                    if previous != direction:
                        raise ValidationError(
                            f"primitive {cell_type!r} has inconsistent pin direction"
                        )
                    if isinstance(bit, int) and not isinstance(bit, bool):
                        batch.append((
                            bit, endpoint_seq, safe_name, port,
                            int(direction == "output"),
                        ))
                        endpoint_seq += 1
                        if len(batch) >= 8192:
                            connection.executemany(
                                "INSERT INTO endpoints VALUES (?,?,?,?,?)", batch
                            )
                            batch.clear()
                current = next(atom_iterator, None)
            if current is not None:
                raise ValidationError(
                    "mapped JSON does not cover every OpenPARF atom"
                )
            if batch:
                connection.executemany(
                    "INSERT INTO endpoints VALUES (?,?,?,?,?)", batch
                )
                batch.clear()
            connection.execute(
                "CREATE INDEX endpoints_net_order ON endpoints(net,seq)"
            )
            connection.commit()
        except (OverflowError, sqlite3.DatabaseError) as error:
            raise ValidationError(
                "OpenPARF endpoint relation cannot represent the mapped netlist"
            ) from error

        library = _bookshelf_library(ports_by_type, platform_mux_types)

        materialized_path = (
            Path(temporary) / "design.nets"
            if nets_path is None else nets_path
        )
        materialized_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{materialized_path.name}.",
            suffix=".tmp",
            dir=materialized_path.parent,
        )
        os.close(descriptor)
        temporary_output = Path(temporary_name)
        emitted = 0
        dropped_single_endpoint = 0
        try:
            with temporary_output.open("w", encoding="utf-8") as stream:
                active_net: Optional[int] = None
                pins: List[Tuple[str, str, bool]] = []

                def emit_net() -> None:
                    nonlocal emitted, dropped_single_endpoint
                    if not pins:
                        return
                    if len(pins) < 2:
                        dropped_single_endpoint += 1
                        return
                    drivers = [pin for pin in pins if pin[2]]
                    if len(drivers) > 1:
                        raise ValidationError(
                            "mapped net has multiple atomic-resource drivers"
                        )
                    ordered = [
                        *drivers, *(pin for pin in pins if not pin[2])
                    ]
                    stream.write(f"net n{emitted} {len(ordered)}\n")
                    for instance, port, _is_driver in ordered:
                        stream.write(f"  {instance} {port}\n")
                    stream.write("endnet\n")
                    emitted += 1

                for row in connection.execute(
                    "SELECT net,instance,port,is_driver FROM endpoints "
                    "ORDER BY net,seq"
                ):
                    net = int(row[0])
                    if active_net is None:
                        active_net = net
                    elif net != active_net:
                        emit_net()
                        pins.clear()
                        active_net = net
                    pins.append((str(row[1]), str(row[2]), bool(row[3])))
                emit_net()
            os.replace(temporary_output, materialized_path)
        except BaseException:
            temporary_output.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

        nets = (
            materialized_path.read_text(encoding="utf-8")
            if nets_path is None else None
        )
        return library, nets, emitted, dropped_single_endpoint


def _complete_clock_region_boxes(
    regions: Mapping[str, Dict[str, Any]],
    width: int,
    height: int,
    *,
    y_axis_order: str,
) -> None:
    """Tile the dense grid, assigning only empty seam coordinates to regions.

    Real sites retain their declared region. No site or resource capacity is
    created in a seam. Native region indices follow increasing dense geometry
    even when physical tile-row order has been reversed for carry chains.
    """
    axes = []
    for axis, extent in enumerate((width, height)):
        observed = {}
        for entry in regions.values():
            key = entry["index"][axis]
            values = [xy[axis] for xy in entry["sites"]]
            low, high = min(values), max(values)
            old = observed.get(key, (low, high))
            observed[key] = min(old[0], low), max(old[1], high)
        ordered = sorted(observed, key=lambda key: observed[key])
        starts = [0]
        for previous, current in zip(ordered, ordered[1:]):
            if observed[previous][1] >= observed[current][0]:
                raise ValidationError("ArchitectureDB clock regions are not rectangular bands")
            starts.append(
                observed[previous][1] + 1
                if axis == 1 and y_axis_order == "descending"
                else observed[current][0]
            )
        bounds = {}
        for index, key in enumerate(ordered):
            low = starts[index]
            high = starts[index + 1] - 1 if index + 1 < len(starts) else extent - 1
            if low > observed[key][0] or high < observed[key][1]:
                raise ValidationError("clock region ownership would move a real site")
            bounds[key] = index, low, high
        axes.append(bounds)
    for entry in regions.values():
        x, xl, xh = axes[0][entry["index"][0]]
        y, yl, yh = axes[1][entry["index"][1]]
        entry["index"] = (x, y)
        entry["bbox"] = (xl, yl, xh, yh)


def _render_site_geometry(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
    *,
    y_axis_order: str = "ascending",
) -> Tuple[str, str, Dict[str, Any]]:
    grouped: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for site, resources in sites:
        coordinate = _physical_coordinate(site)
        region = site.get("physical_region")
        if region is not None:
            if (
                not isinstance(region, Mapping)
                or not isinstance(region.get("slr"), str)
                or not region["slr"]
                or not isinstance(region.get("clock_region"), str)
                or not region["clock_region"]
            ):
                raise ValidationError(
                    "ArchitectureDB physical_region is incomplete"
                )
            region = {
                "slr": region["slr"],
                "clock_region": region["clock_region"],
            }
        group = grouped.setdefault(coordinate, {
            "resources": Counter(), "physical_sites": defaultdict(list),
            "physical_region": region,
        })
        if group["physical_region"] != region:
            raise ValidationError(
                "ArchitectureDB sites at one physical tile disagree on region"
            )
        group["resources"].update(resources)
        tile = site.get("tile")
        site_index = (
            tile.get("site_index", 0) if isinstance(tile, Mapping) else 0
        )
        if isinstance(site_index, bool) or not isinstance(site_index, int):
            raise ValidationError("ArchitectureDB site_index is invalid")
        for resource, count in resources.items():
            if count <= 0:
                continue
            # Logic capacities are BEL counts within one physical slice; hard
            # capacities are counts of independently selectable physical sites.
            if resource in {"LUT", "FF"}:
                if group["physical_sites"][resource]:
                    raise ValidationError(
                        "ArchitectureDB has multiple logic sites at one physical tile"
                    )
                group["physical_sites"][resource].append(
                    (site_index, site["name"])
                )
            else:
                group["physical_sites"][resource].append(
                    (site_index, site["name"])
                )
    for group in grouped.values():
        for resource, indexed_names in group["physical_sites"].items():
            group["physical_sites"][resource] = [
                name for _index, name in sorted(indexed_names)
            ]

    coordinates = list(grouped)
    x_axis = sorted({coordinate[0] for coordinate in coordinates})
    if y_axis_order not in {"ascending", "descending"}:
        raise ValidationError("OpenPARF physical Y-axis order is invalid")
    physical_y_values = {coordinate[1] for coordinate in coordinates}
    # Preserve empty physical tile rows.  UltraScale+ has real seams between
    # directed CARRY_NEXT chains (for example at an SLR boundary); compressing
    # the observed rows made the two sides adjacent in Bookshelf coordinates
    # and let the native carry legalizer cross a nonexistent dedicated edge.
    # Sparse rows are already supported by the patched OpenPARF site map and
    # ISM operators, so retain the complete integer grid span here.
    minimum_y = min(physical_y_values)
    maximum_y = max(physical_y_values)
    # RapidWright's UltraScale+ inventory has empty inter-SLR seam rows.  The
    # observed sites in consecutive SLRs start at a constant physical pitch,
    # while the final seam has no site from which the old code could discover
    # its extent.  Retain the internal seams (needed by CARRY_NEXT legality)
    # and extend the final selected SLR to the same pitch.  Without this, the
    # SLR boxes were separated by holes and the Bookshelf SLR grid failed only
    # after an expensive per-partition mapping run.
    slr_minimum_y: Dict[str, int] = {}
    for coordinate, group in grouped.items():
        region = group.get("physical_region")
        if isinstance(region, Mapping) and isinstance(region.get("slr"), str):
            slr = region["slr"]
            slr_minimum_y[slr] = min(
                coordinate[1], slr_minimum_y.get(slr, coordinate[1])
            )
    ordered_slr_starts = sorted(set(slr_minimum_y.values()))
    if len(ordered_slr_starts) > 1:
        pitches = {
            right - left
            for left, right in zip(ordered_slr_starts, ordered_slr_starts[1:])
        }
        if len(pitches) != 1:
            raise ValidationError("ArchitectureDB SLR row pitch is inconsistent")
        pitch = next(iter(pitches))
        expected_maximum_y = minimum_y + pitch * len(ordered_slr_starts) - 1
        if maximum_y > expected_maximum_y:
            raise ValidationError("ArchitectureDB SLR sites exceed their row pitch")
        maximum_y = expected_maximum_y
    y_axis = list(range(minimum_y, maximum_y + 1))
    if y_axis_order == "descending":
        y_axis.reverse()
    x_index = {value: index for index, value in enumerate(x_axis)}
    y_index = {value: index for index, value in enumerate(y_axis)}
    signatures = sorted({
        tuple(sorted(
            group["resources"].items(),
            key=lambda item: _resource_sort_key(item[0]),
        ))
        for group in grouped.values()
    })
    signature_names = {
        signature: f"EMUFLOW_SITE_{index}"
        for index, signature in enumerate(signatures)
    }
    lines = []
    for signature in signatures:
        lines.append(f"SITE {signature_names[signature]}")
        lines.extend(f"  {resource} {count}" for resource, count in signature)
        lines.extend(["END SITE", ""])
    site_prefix = "\n".join(lines) + "\n"
    lines = ["END RESOURCES", "", f"SITEMAP {len(x_axis)} {len(y_axis)}"]
    site_map = []
    # Bookshelf requires site records in increasing dense coordinate order;
    # physical tile-row order may be reversed to align dense Y with a directed
    # native cascade graph.
    for coordinate, group in sorted(
        grouped.items(),
        key=lambda item: (
            x_index[item[0][0]], y_index[item[0][1]], item[0]
        ),
    ):
        resources = group["resources"]
        dense = (x_index[coordinate[0]], y_index[coordinate[1]])
        physical_sites = {
            resource: list(names)
            for resource, names in sorted(
                group["physical_sites"].items(),
                key=lambda item: _resource_sort_key(item[0]),
            )
        }
        all_names = sorted({
            name for names in physical_sites.values() for name in names
        })
        site_map.append({
            "dense_x": dense[0], "dense_y": dense[1],
            "physical_x": coordinate[0], "physical_y": coordinate[1],
            "site": all_names[0],
            "resources": dict(sorted(resources.items())),
            "physical_sites": physical_sites,
            "physical_region": group["physical_region"],
        })
    # OpenPARF extends each entry to the next entry in its column. Terminate
    # each logic site after one row: physical gaps are not extra placement
    # area. Hard-resource spans retain their native height.
    # Legalizer position tensors use the center of that box, while the emitted
    # .pl file is written back using its lower-left dense coordinate.  Preserve
    # both coordinate systems explicitly so typed hard blocks do not confuse
    # the serialized site identity with the in-core legal position.
    sites_by_x: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for item in site_map:
        sites_by_x[item["dense_x"]].append(item)
    blocked_rows = False
    for column in sites_by_x.values():
        column.sort(key=lambda item: item["dense_y"])
        for index, item in enumerate(column):
            next_y = (
                column[index + 1]["dense_y"]
                if index + 1 < len(column)
                else len(y_axis)
            )
            item["placement_x"] = item["dense_x"] + 0.5
            logic_site = any(
                item["resources"].get(resource, 0) for resource in ("LUT", "FF")
            )
            upper_y = item["dense_y"] + 1 if logic_site else next_y
            item["placement_y"] = (item["dense_y"] + upper_y) * 0.5
            signature = tuple(sorted(
                item["resources"].items(),
                key=lambda entry: _resource_sort_key(entry[0]),
            ))
            lines.append(
                f"{item['dense_x']} {item['dense_y']} {signature_names[signature]}"
            )
            if upper_y < next_y:
                blocked_rows = True
                lines.append(
                    f"{item['dense_x']} {upper_y} EMUFLOW_UNAVAILABLE"
                )
    if blocked_rows:
        site_prefix += "SITE EMUFLOW_UNAVAILABLE\nEND SITE\n\n"
    site_prefix += "RESOURCES\n"
    lines.append("END SITEMAP")
    present_regions = [
        item["physical_region"] for item in site_map
        if item["physical_region"] is not None
    ]
    if present_regions and len(present_regions) != len(site_map):
        raise ValidationError(
            "ArchitectureDB clock-region coverage is partial"
        )
    clock_region_contract = None
    slr_contract = None
    if present_regions:
        by_name: Dict[str, Dict[str, Any]] = {}
        for item in site_map:
            region = item["physical_region"]
            name = region["clock_region"]
            match = _CLOCK_REGION_NAME.fullmatch(name)
            if match is None:
                raise ValidationError(
                    f"ArchitectureDB clock region {name!r} is not canonical"
                )
            index = (int(match.group(1)), int(match.group(2)))
            entry = by_name.setdefault(name, {
                "index": index, "slr": region["slr"], "sites": [],
            })
            if entry["index"] != index or entry["slr"] != region["slr"]:
                raise ValidationError(
                    f"ArchitectureDB clock region {name!r} is inconsistent"
                )
            entry["sites"].append((item["dense_x"], item["dense_y"]))
        minimum_x = min(entry["index"][0] for entry in by_name.values())
        minimum_y = min(entry["index"][1] for entry in by_name.values())
        for entry in by_name.values():
            entry["source_index"] = entry["index"]
            entry["index"] = (
                entry["index"][0] - minimum_x,
                entry["index"][1] - minimum_y,
            )
        width = max(entry["index"][0] for entry in by_name.values()) + 1
        height = max(entry["index"][1] for entry in by_name.values()) + 1
        expected = {(x, y) for x in range(width) for y in range(height)}
        observed = {entry["index"] for entry in by_name.values()}
        if observed != expected:
            raise ValidationError(
                "ArchitectureDB clock-region grid is incomplete"
            )
        _complete_clock_region_boxes(
            by_name, len(x_axis), len(y_axis), y_axis_order=y_axis_order,
        )
        for item in site_map:
            name = item["physical_region"]["clock_region"]
            xl, yl, xh, yh = by_name[name]["bbox"]
            if not (xl <= item["dense_x"] <= xh and yl <= item["dense_y"] <= yh):
                raise ValidationError(f"ArchitectureDB clock region {name!r} is not rectangular")
        contract_regions = []
        lines.extend(["", f"CLOCKREGIONS {width} {height}"])
        for name, entry in sorted(
            by_name.items(), key=lambda item: item[1]["index"]
        ):
            exported_name = f"X{entry['index'][0]}Y{entry['index'][1]}"
            xl, yl, xh, yh = entry["bbox"]
            ymid = yl + (yh - yl + 1) // 2
            lines.append(
                f"  CLOCKREGION {exported_name} : "
                f"{xl} {yl} {xh} {yh} {ymid} {xl}"
            )
            contract_region = {
                "name": exported_name, "x": entry["index"][0],
                "y": entry["index"][1], "slr": entry["slr"],
                "bbox": [xl, yl, xh, yh],
            }
            if exported_name != name:
                contract_region["source_name"] = name
            contract_regions.append(contract_region)
        lines.append("END CLOCKREGIONS")
        clock_region_contract = {
            "width": width, "height": height,
            "geometry": "complete-dense-grid-empty-seams-no-fabricated-sites-v1",
            "maximum_clocks_per_region": _MAX_CLOCKS_PER_REGION,
            # The checked-in UTPlaceFX clock planner recognizes a single
            # SLICE/DSP/RAM site class and discovers clocks only from explicit
            # clock-source models.  A real UltraScale+ database contains
            # multiple BRAM views plus URAM, while this adapter intentionally
            # rejects BUFG/clock-source primitives today.  Export the exact
            # region geometry so the parser and later independent validator
            # can consume it, but do not silently claim active clock planning.
            "native_enforcement": "disabled",
            "native_enforcement_reason": (
                "OpenPARF UTPlaceFX cannot safely represent the current "
                "multi-resource UltraScale+ site model or discover its clock "
                "sources"
            ),
            "regions": contract_regions,
        }
        by_slr: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for item in site_map:
            slr = item["physical_region"].get("slr")
            if not isinstance(slr, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_\-]*", slr
            ):
                raise ValidationError(
                    f"ArchitectureDB SLR name {slr!r} is not canonical"
                )
            by_slr[slr].append((item["dense_x"], item["dense_y"]))
        slr_bboxes: Dict[str, Tuple[int, int, int, int]] = {}
        for slr, coordinates in by_slr.items():
            xs = [coordinate[0] for coordinate in coordinates]
            ys = [coordinate[1] for coordinate in coordinates]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            for item in site_map:
                if (
                    bbox[0] <= item["dense_x"] <= bbox[2]
                    and bbox[1] <= item["dense_y"] <= bbox[3]
                    and item["physical_region"]["slr"] != slr
                ):
                    raise ValidationError(
                        f"ArchitectureDB SLR {slr!r} is not rectangular"
                    )
            slr_bboxes[slr] = bbox
        # Expand an SLR's observed-site bbox over its following empty seam.
        # The physical sites remain unchanged; only the coarse SLR ownership
        # grid covers every Bookshelf row.  Constant pitch and equal-sized SLRs
        # are still checked below, so malformed region metadata fails closed.
        ordered_slrs = sorted(
            slr_bboxes,
            key=lambda name: (slr_bboxes[name][1], name),
        )
        if len(ordered_slrs) > 1:
            starts = [slr_bboxes[name][1] for name in ordered_slrs]
            pitches = {
                right - left for left, right in zip(starts, starts[1:])
            }
            if len(pitches) != 1:
                raise ValidationError("ArchitectureDB SLR y-axis pitch is inconsistent")
            pitch = next(iter(pitches))
            for index, name in enumerate(ordered_slrs):
                xl, yl, xh, observed_yh = slr_bboxes[name]
                # Dense Y may follow the native cascade direction.  With a
                # descending axis, the implicit final physical seam appears
                # *before* the first observed dense SLR row rather than after
                # the last one.  Anchor ownership spans to the complete dense
                # device extent instead of to the first observed site so both
                # orientations cover exactly the same constant-pitch grid.
                expanded_yl = index * pitch
                expanded_yh = expanded_yl + pitch - 1
                if yl < expanded_yl or observed_yh > expanded_yh:
                    raise ValidationError(
                        f"ArchitectureDB SLR {name!r} exceeds its y-axis pitch"
                    )
                slr_bboxes[name] = (xl, expanded_yl, xh, expanded_yh)
        x_spans = sorted({(bbox[0], bbox[2]) for bbox in slr_bboxes.values()})
        y_spans = sorted({(bbox[1], bbox[3]) for bbox in slr_bboxes.values()})
        expected_slr_bboxes = {
            (x_span[0], y_span[0], x_span[1], y_span[1])
            for x_span in x_spans for y_span in y_spans
        }
        if set(slr_bboxes.values()) != expected_slr_bboxes:
            raise ValidationError("ArchitectureDB SLR grid is incomplete")

        def validate_axis_spans(
            spans: Sequence[Tuple[int, int]], extent: int, axis: str,
        ) -> int:
            if not spans or spans[0][0] != 0 or spans[-1][1] != extent - 1:
                raise ValidationError(
                    f"ArchitectureDB SLR {axis}-axis does not cover the device"
                )
            for previous, current in zip(spans, spans[1:]):
                if previous[1] + 1 != current[0]:
                    raise ValidationError(
                        f"ArchitectureDB SLR {axis}-axis is not contiguous"
                    )
            sizes = {end - start + 1 for start, end in spans}
            if len(sizes) != 1:
                raise ValidationError(
                    "OpenPARF requires equal-size rectangular SLRs"
                )
            return next(iter(sizes))

        slr_width = validate_axis_spans(x_spans, len(x_axis), "x")
        slr_height = validate_axis_spans(y_spans, len(y_axis), "y")
        bbox_to_slr = {bbox: slr for slr, bbox in slr_bboxes.items()}
        slr_regions = []
        lines.extend(["", f"SUPERLOGICREGIONS {len(x_spans)} {len(y_spans)}"])
        for slr_x, x_span in enumerate(x_spans):
            for slr_y, y_span in enumerate(y_spans):
                bbox = (x_span[0], y_span[0], x_span[1], y_span[1])
                slr = bbox_to_slr[bbox]
                lines.append(
                    "  SUPERLOGICREGION "
                    f"{slr} ULTRASCALE_PLUS : {slr_x} {slr_y} "
                    f"{slr_width} {slr_height}"
                )
                slr_regions.append({
                    "name": slr, "x": slr_x, "y": slr_y,
                    "bbox": list(bbox),
                })
        lines.append("END SUPERLOGICREGIONS")
        slr_contract = {
            "width": len(x_spans), "height": len(y_spans),
            "native_enforcement": (
                "enabled" if len(slr_regions) > 1 else "not-required"
            ),
            "regions": slr_regions,
        }
    return site_prefix, "\n".join(lines) + "\n", {
        "x_axis": x_axis, "y_axis": y_axis,
        "y_axis_order": y_axis_order, "sites": site_map,
        "clock_regions": clock_region_contract,
        "super_logic_regions": slr_contract,
    }


def _render_sites(
    device_static: Mapping[str, Any],
    atoms: Sequence[Mapping[str, str]],
    *,
    platform_mux_types: Sequence[str] = (),
) -> str:
    """Add partition-specific model declarations to shared device geometry."""

    models_by_resource: Dict[str, set[str]] = defaultdict(set)
    for atom in atoms:
        models_by_resource[atom["resource"]].add(atom["cell_type"])
    for cell_type in platform_mux_types:
        models_by_resource[_MUX_RESOURCES[cell_type]].add(cell_type)
    resources = "".join(
        f"  {resource} {' '.join(sorted(models))}\n"
        for resource, models in sorted(
            models_by_resource.items(),
            key=lambda item: _resource_sort_key(item[0]),
        )
    )
    return (
        str(device_static["site_prefix"])
        + resources
        + str(device_static["site_suffix"])
    )


def _device_static_geometry(
    architecture: ArchitectureDB,
    hard_resources: Sequence[str],
    mux_resources: Sequence[str],
    *,
    require_carry8: bool,
    native: Optional[Mapping[str, Any]] = None,
    allowed_slrs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build immutable device geometry once across parallel FPGA workers."""

    y_axis_order = _native_carry_y_axis_order(architecture, native)
    selected_slrs = tuple(allowed_slrs) if allowed_slrs is not None else None
    key = (
        architecture,
        tuple(sorted(hard_resources)),
        tuple(sorted(mux_resources)),
        bool(require_carry8),
        y_axis_order,
        selected_slrs,
    )
    # The derivation is Python/GIL bound and produces a multi-GiB object for a
    # complete XCVU19P. Holding the lock during the first build prevents a
    # second physical worker from repeating the same scan and doubling RSS.
    with _DEVICE_STATIC_CACHE_LOCK:
        cached = _DEVICE_STATIC_CACHE.get(key)
        if cached is not None:
            _DEVICE_STATIC_CACHE.move_to_end(key)
            return cached
        sites = _placement_sites(
            architecture,
            hard_resources,
            mux_resources,
            require_carry8=require_carry8,
        )
        if selected_slrs is not None:
            if not selected_slrs or len(set(selected_slrs)) != len(selected_slrs):
                raise ValidationError("OpenPARF allowed SLR list is empty or duplicated")
            known_slrs = {
                region.get("slr")
                for site, _resources in sites
                if isinstance((region := site.get("physical_region")), Mapping)
                and isinstance(region.get("slr"), str)
            }
            unknown = set(selected_slrs) - known_slrs
            if unknown:
                raise ValidationError(
                    f"OpenPARF allowed SLR list contains unknown regions {sorted(unknown)!r}"
                )
            sites = [
                item for item in sites
                if isinstance(item[0].get("physical_region"), Mapping)
                and item[0]["physical_region"].get("slr") in selected_slrs
            ]
        placement_region = _validate_native_placement_region(sites)
        sites, site_headroom_contract = _expose_all_clock_region_sites(sites)
        placement_region = {
            **placement_region,
            "available_logic_sites": sum(
                "LUT" in resources and "FF" in resources
                for _site, resources in sites
            ),
        }
        if selected_slrs is not None:
            placement_region["allowed_slrs"] = list(selected_slrs)
        site_prefix, site_suffix, coordinate_system = _render_site_geometry(
            sites, y_axis_order=y_axis_order
        )
        value: Dict[str, Any] = {
            "sites": sites,
            "placement_region": placement_region,
            "site_prefix": site_prefix,
            "site_suffix": site_suffix,
            "coordinate_system": coordinate_system,
            "site_database": None,
            "site_count": len(coordinate_system["sites"]),
            "site_headroom_contract": site_headroom_contract,
        }
        _DEVICE_STATIC_CACHE[key] = value
        _DEVICE_STATIC_CACHE.move_to_end(key)
        while len(_DEVICE_STATIC_CACHE) > _DEVICE_STATIC_CACHE_LIMIT:
            _DEVICE_STATIC_CACHE.popitem(last=False)
        return value


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


def export_xilinx_openparf_atomic(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[ArchitectureDB] = None,
    allowed_slrs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Export the fail-closed native mixed-resource qualification subset."""

    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    if packed_value is None:
        candidate = read_json(packed_path)
        packed = (
            read_xilinx_openparf_atomic_source(
                packed_path, mapped_path=mapped_path, mapped_value=mapped,
                mapped_sha256=_sha256(mapped_path),
            )
            if isinstance(candidate, Mapping)
            and candidate.get("schema") == OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
            else candidate
        )
    else:
        packed = packed_value
    if (
        not isinstance(packed, Mapping)
        or packed.get("schema") not in {
            PACKED_SITE_NETLIST_SCHEMA, OPENPARF_ATOMIC_SOURCE_SCHEMA,
        }
    ):
        raise ValidationError("OpenPARF atomic source header is invalid")
    if packed.get("schema") == OPENPARF_ATOMIC_SOURCE_SCHEMA:
        source = packed.get("source")
        if (
            packed.get("status") != "pass"
            or not isinstance(source, Mapping)
            or source.get("mapped_sha256") != _sha256(mapped_path)
        ):
            raise ValidationError("OpenPARF atomic source identity is invalid")
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    typed_hardblock_mode = (
        native_constraints_path is not None or provider_manifest_path is not None
    )
    if typed_hardblock_mode and (
        native_constraints_path is None or provider_manifest_path is None
    ):
        raise ValidationError(
            "typed hardblock route requires both native constraints and provider manifest"
        )
    native = None
    if typed_hardblock_mode:
        native, _native_report = load_xilinx_native_device_constraints(
            native_constraints_path,
            architecture_path=architecture_path,
            provider_manifest_path=provider_manifest_path,
        )
    selected_module_name, selected_module = _select_module(
        mapped, top if top is not None else packed.get("top")
    )
    selected_cells = selected_module.get("cells", {})
    has_site_macro = any(
        isinstance(cell, Mapping)
        and cell.get("type") in set(_MUX_RESOURCES) | {
            "CARRY8", DUAL_OUTPUT_LUT_TYPE,
        }
        for cell in selected_cells.values()
    )
    macro_contract = (
        build_xilinx_physical_macro_contract(
            mapped_path, top=selected_module_name, source=mapped
        )
        if has_site_macro else {"site_macros": []}
    )
    selected_top, cells, atoms = _collect_atoms(
        mapped, packed, top if top is not None else packed.get("top"),
        allow_hardblock_cascades=typed_hardblock_mode,
        physical_macro_contract=macro_contract,
    )
    hard = sorted({
        atom["cell_type"] for atom in atoms
        if atom["cell_type"] in _HARD_RESOURCES
    })
    demanded_mux = {
        atom["cell_type"] for atom in atoms
        if atom["cell_type"] in _MUX_RESOURCES
    }
    # OpenPARF identifies the UltraScale Bookshelf grammar from the complete
    # slice-mux resource hierarchy.  Emitting only the mux levels present in a
    # particular mapped design makes (for example) an F7/F8-only design look
    # like a legacy architecture; the parser then rejects the following
    # SUPERLOGICREGIONS section.  Device geometry is a platform property, so
    # once any native slice mux is used retain F7/F8/F9 capacity regardless of
    # whether the design happens to instantiate the top level.
    mux = sorted(_MUX_RESOURCES if demanded_mux else ())
    has_carry8 = any(atom["cell_type"] == "CARRY8" for atom in atoms)
    device_static = _device_static_geometry(
        architecture, hard, mux, require_carry8=has_carry8, native=native,
        allowed_slrs=allowed_slrs,
    )
    sites = device_static["sites"]
    placement_region = device_static["placement_region"]
    per_site_capacity, density_contract, target_density = (
        _validate_native_density_contract(sites, atoms)
    )
    demand = Counter(atom["resource"] for atom in atoms)
    capacity = Counter()
    for _site, resources in sites:
        capacity.update(resources)
    # The native CLB model exposes 16 LUT resource units for two-LUT BLE
    # compatibility, but EmuFlow's conservative architecture policy permits
    # only eight independent 6LUT BELs.  The importer enforces even z slots.
    capacity["LUT"] = _QUALIFIED_LUTS_PER_SLICE * sum(
        "LUT" in resources for _site, resources in sites
    )
    for resource, count in demand.items():
        if count > capacity[resource]:
            raise ValidationError(
                f"atomic demand {count} exceeds {resource} capacity {capacity[resource]}"
            )
    names = _OrdinalSafeNames(atoms)
    output_dir.mkdir(parents=True, exist_ok=True)
    library, materialized_nets, net_count, dropped_single_endpoint_nets = (
        _render_library_and_nets(
            cells,
            atoms,
            names,
            platform_mux_types=mux,
            nets_path=output_dir / "design.nets",
        )
    )
    if materialized_nets is not None:
        raise AssertionError("production net export unexpectedly materialized text")
    coordinate_system = device_static["coordinate_system"]
    site_text = _render_sites(device_static, atoms, platform_mux_types=mux)
    node_path = output_dir / "design.nodes"
    with node_path.open("w", encoding="utf-8") as stream:
        for index, atom in enumerate(atoms):
            stream.write(f"a{index} {atom['cell_type']}\n")
    files = {
        "design.lib": library,
        "design.scl": site_text,
        "design.pl": "",
        "design.aux": "design : design.nodes design.nets design.pl design.scl design.lib\n",
    }
    for name, text in files.items():
        (output_dir / name).write_text(text, encoding="utf-8")
    file_names = ["design.nodes", "design.nets", *files]
    model_map = {}
    for primitive in sorted(
        {atom["cell_type"] for atom in atoms}, key=_primitive_sort_key
    ):
        resource = (
            "LUT" if primitive in _SLICE_LUT_TYPES
            else "FF" if primitive in FF_TYPES
            else "CARRY8" if primitive == "CARRY8"
            else _MUX_RESOURCES[primitive] if primitive in _MUX_RESOURCES
            else _HARD_RESOURCES[primitive]
        )
        unit_dimension = 1.0 / math.sqrt(per_site_capacity[resource])
        if primitive in FF_TYPES:
            model_map[primitive] = {
                "FF": [unit_dimension, unit_dimension], "isFF": 1,
            }
        elif primitive in _SLICE_LUT_TYPES:
            model_map[primitive] = {
                "LUT": [unit_dimension, unit_dimension],
                "isLUT": (
                    6 if primitive == DUAL_OUTPUT_LUT_TYPE
                    else int(primitive[3:])
                ),
            }
        elif primitive == "CARRY8":
            model_map[primitive] = {
                "CARRY8": [unit_dimension, unit_dimension]
            }
        elif primitive in _MUX_RESOURCES:
            model_map[primitive] = {
                _MUX_RESOURCES[primitive]: [unit_dimension, unit_dimension]
            }
        else:
            model_map[primitive] = {
                _HARD_RESOURCES[primitive]: [unit_dimension, unit_dimension]
            }
    resource_map = {
        resource: [resource]
        for resource in sorted(demand, key=_resource_sort_key)
    }
    resource_categories = {
        resource: (
            "LUTL" if resource == "LUT"
            else "FF" if resource == "FF"
            else "Carry" if resource == "CARRY8"
            else "SSSIR"
        )
        for resource in sorted(demand, key=_resource_sort_key)
    }
    config = {
        "benchmark_name": "xilinx_atomic_mixed_resource",
        "benchmark_format": "bookshelf", "architecture_name": "ultrascale",
        "aux_input": str((output_dir / "design.aux").resolve()),
        "gpu": 0, "dtype": "float64", "target_density": target_density,
        # Match the upstream ISPD reference recipes. OpenPARF's generic
        # default stops global placement at overflow 0.20, but its native
        # routability adjustment is only eligible at overflow <= 0.15. If
        # stop_overflow is implicit, RUDY/pin inflation is enabled in the
        # configuration yet never executes on a realistic design.
        "stop_overflow": _GLOBAL_PLACEMENT_STOP_OVERFLOW,
        "random_seed": 1000, "max_global_place_iters": 2000,
        # The first density-feasible iterate after RUDY/pin inflation can be
        # a transient HPWL spike.  Require a bounded feasible settling window
        # and restore the best feasible global placement before native
        # legalization/detailed placement.
        "emuflow_stable_global_placement": True,
        "emuflow_min_feasible_iterations": 96,
        "emuflow_convergence_patience": 64,
        "emuflow_relative_hpwl_improvement": 1.0e-4,
        # Overflow 0.10 is a routability target rather than a physical
        # legality theorem.  Preserve a bounded 0.65 candidate so DirectLG
        # can provide exact displacement and legality evidence on one
        # representative FPGA without weakening the normal pass criterion.
        "emuflow_diagnostic_handoff_overflow": (
            _DIAGNOSTIC_HANDOFF_OVERFLOW
        ),
        "emuflow_diagnostic_handoff_patience": (
            _DIAGNOSTIC_HANDOFF_PATIENCE
        ),
        # Area inflation resets the nonlinear optimizer.  Do not begin a late
        # RUDY/pin adjustment unless the bounded run can still provide a full
        # settling interval plus the stable feasible window.
        "emuflow_post_adjustment_iteration_reserve": (
            _POST_ADJUSTMENT_ITERATION_RESERVE
        ),
        "emuflow_post_adjustment_feedback_samples": (
            _POST_ADJUSTMENT_FEEDBACK_SAMPLES
        ),
        # The atomic/native path must use the same fanout-aware analytical
        # objective as the continuous guidance path.  Without these weights,
        # a reset or enable net with tens of thousands of sinks contributes
        # one bounding box to HPWL even though detailed routing must build the
        # complete distribution tree.
        "emuflow_high_fanout_weight_threshold": _HIGH_FANOUT_WEIGHT_THRESHOLD,
        "emuflow_high_fanout_weight_exponent": _HIGH_FANOUT_WEIGHT_EXPONENT,
        "emuflow_high_fanout_weight_maximum": _HIGH_FANOUT_WEIGHT_MAXIMUM,
        "global_place_flag": 1, "legalize_flag": 1,
        "detailed_place_flag": 1, "generic_cluster_placement_flag": 0,
        "logic_area_type_names": ["LUT", "FF"],
        "emuflow_expected_logic_site_area": {
            resource: density_contract[resource]["placeable_area"]
            for resource in ("LUT", "FF") if resource in density_contract
        },
        "plot_flag": 0,
        "plot_target_at_names": sorted(demand, key=_resource_sort_key),
        "io_at_names": [], "num_threads": 8,
        "gp_model2area_types_map": model_map,
        # Eight qualified LUT BELs and sixteen FF BELs share one slice.
        # Median-cell fillers would therefore materialize millions of
        # Python/Torch rows on XCVU19P.  Treat this as a preferred row limit,
        # then raise the count as needed to keep each logic sample within half
        # an analytical density bin.  The finer field sampling avoids a false
        # overflow plateau while retaining far fewer rows than natural sizing.
        "gp_max_fillers_per_area_type": {
            resource: (
                _LOGIC_FILLER_LIMIT if resource in {"LUT", "FF"} else 1
            )
            for resource in sorted(demand, key=_resource_sort_key)
        },
        "gp_max_logic_filler_bin_span": 0.5,
        "gp_resource2area_types_map": resource_map,
        "resource_categories": resource_categories,
        "CLB_capacity": 16, "BLE_capacity": 2,
        # The source-sealed UltraScale+ template qualifies eight independent
        # 6LUT BELs.  It does not qualify arbitrary cells for the paired 5LUT
        # position, so native direct legalization must never consume it.
        "allow_paired_luts": 0,
        "num_ControlSets_per_CLB": 2,
        # OpenPARF's native ISPD flow applies RUDY- and pin-utilization-aware
        # inflation to LUT/FF area types before exact legalization.  Leaving
        # this disabled is not a neutral simplification: a sparse whole-device
        # design can collapse into a few locally full clock regions, producing
        # a legal placement with catastrophic negotiated-routing overlap.
        # Keep hard blocks out of this analytical adjustment; their exact
        # source-sealed windows remain owned by the typed macro legalizer.
        "gp_adjust_area": 1, "gp_adjust_area_types": ["LUT", "FF"],
        "gp_max_adjust_area_iters": _ROUTABILITY_ADJUSTMENT_ITERATIONS,
        "gp_adjust_area_overflow_threshold": (
            _ROUTABILITY_ADJUSTMENT_OVERFLOW_THRESHOLD
        ),
        "gp_adjust_route_area": 1,
        "gp_adjust_area_route_opt_adjust_exponent": (
            _ROUTE_AREA_ADJUSTMENT_EXPONENT
        ),
        "gp_adjust_area_max_route_opt_adjust_rate": (
            _MAX_ROUTE_AREA_ADJUSTMENT_RATE
        ),
        "gp_adjust_pin_area": 1,
        "gp_adjust_area_max_pin_opt_adjust_rate": (
            _MAX_PIN_AREA_ADJUSTMENT_RATE
        ),
        "gp_adjust_resource_area": 0,
        # CLOCKREGIONS remains part of the source-sealed Bookshelf database,
        # but native enforcement stays off until clock-source primitives and
        # the multi-resource device model are represented without collapsing
        # BRAM/URAM classes.  Enabling UTPlaceFX here would either abort or
        # enforce the wrong capacity model.
        "honor_clock_region_constraints": 0,
        "honor_half_column_constraints": 0,
        "confine_clock_region_flag": 0,
        "count_ck_cr": 0,
        "maximum_clock_per_clock_region": _MAX_CLOCKS_PER_REGION,
        "maximum_clock_per_half_column": 0,
        "clock_region_capacity": _MAX_CLOCKS_PER_REGION,
        "route_flag": 0,
        "slr_aware_flag": int(
            coordinate_system["super_logic_regions"] is not None
            and len(coordinate_system["super_logic_regions"]["regions"]) > 1
        ),
        # LEAPS Equation (23) applies this as a fixed Adam step.  It must not
        # be exponentiated by the absolute SLL-count delta.
        "wlw_psi_step_size": _SLL_ADAM_STEP_SIZE,
        "wlw_delta_sll_ema_weight": _SLL_DELTA_EMA_WEIGHT,
        "result_dir": str((output_dir / "results").resolve()),
    }
    if has_carry8:
        config.update({
            "architecture_type": "ultrascale",
            "carry_chain_module_name": "CARRY8",
            "carry_chain_at_name": "CARRY8",
            "carry_chain_legalization_flag": 1,
            "align_carry_chain_flag": 1,
        })
    hardblock_groups = []
    singleton_window_sets: Dict[str, Dict[str, Any]] = {}
    site_chain_sets: List[Dict[str, Any]] = []
    slice_macros = [
        macro for macro in macro_contract.get("site_macros", [])
        if macro.get("kind") in {
            "muxf7-cone", "muxf8-cone", "muxf9-cone", "carry8-lut6_2",
        }
    ]
    carry_macro_by_instance: Dict[str, Mapping[str, Any]] = {}
    for macro in slice_macros:
        if macro.get("kind") != "carry8-lut6_2":
            continue
        carry_members = [
            member for member in macro.get("members", [])
            if member.get("cell_type") == "CARRY8"
        ]
        if len(carry_members) != 1:
            raise ValidationError(
                f"CARRY8 site macro {macro.get('id')!r} has invalid ownership"
            )
        carry_instance = carry_members[0]["instance"]
        if carry_instance in carry_macro_by_instance:
            raise ValidationError("CARRY8 instance belongs to two site macros")
        carry_macro_by_instance[carry_instance] = macro
    logical_carry_chains = [
        chain for chain in packed.get("cascade_chains", [])
        if chain.get("cell_type") == "CARRY8"
        and isinstance(chain.get("instances"), list)
        and len(chain["instances"]) >= 2
    ]
    cascade_owned_carry = set()
    if logical_carry_chains and typed_hardblock_mode:
        native_carry_family = next((
            family
            for family in native.get("payload", {}).get("dedicated_adjacency", [])
            if isinstance(family, Mapping) and family.get("kind") == "CARRY_NEXT"
        ), None)
        native_carry_chains = (
            native_carry_family.get("chains")
            if isinstance(native_carry_family, Mapping) else None
        )
        if (
            not isinstance(native_carry_chains, list)
            or not native_carry_chains
            or any(
                not isinstance(chain, list) or len(chain) < 2
                or not all(isinstance(site, str) and site for site in chain)
                for chain in native_carry_chains
            )
        ):
            raise ValidationError("native constraints contain no valid CARRY_NEXT chains")
        available_carry_sites = {
            site_name
            for item in coordinate_system["sites"]
            for site_name in item.get("physical_sites", {}).get("LUT", [])
        }
        filtered_native_carry_chains = []
        for physical_chain in native_carry_chains:
            run = []
            for site_name in physical_chain:
                if site_name in available_carry_sites:
                    run.append(site_name)
                    continue
                if len(run) >= 2:
                    filtered_native_carry_chains.append(run)
                run = []
            if len(run) >= 2:
                filtered_native_carry_chains.append(run)
        native_carry_chains = filtered_native_carry_chains
        if not native_carry_chains:
            raise ValidationError(
                "available site domain contains no CARRY_NEXT window"
            )
        chain_set_id = "native:CARRY_NEXT"
        site_chain_sets.append({
            "id": chain_set_id,
            "site_resource": "LUT",
            "chains": native_carry_chains,
        })
        for chain in logical_carry_chains:
            carry_instances = chain["instances"]
            overlap = cascade_owned_carry.intersection(carry_instances)
            if overlap:
                raise ValidationError(
                    f"CARRY8 cascade ownership overlaps: {sorted(overlap)!r}"
                )
            cascade_owned_carry.update(carry_instances)
            try:
                unit_macros = [
                    carry_macro_by_instance[instance]
                    for instance in carry_instances
                ]
            except KeyError as error:
                raise ValidationError(
                    "CARRY8 cascade has no complete full-slice macro"
                ) from error
            unit_signatures = [
                [
                    (
                        member["cell_type"], member["physical_role"],
                        (
                            "LUT" if member["cell_type"] in _SLICE_LUT_TYPES
                            else "CARRY8"
                        ),
                        _slice_role_slot(
                            member["cell_type"], member["physical_role"]
                        ),
                    )
                    for member in macro["members"]
                ]
                for macro in unit_macros
            ]
            if any(
                signature != unit_signatures[0]
                for signature in unit_signatures[1:]
            ):
                raise ValidationError(
                    "CARRY8 cascade units do not share one physical slice template"
                )
            source_instances = [
                member["instance"]
                for macro in unit_macros for member in macro["members"]
            ]
            chain_length = len(unit_macros)
            window_count = sum(
                max(0, len(physical_chain) - chain_length + 1)
                for physical_chain in native_carry_chains
            )
            if window_count <= 0:
                raise ValidationError(
                    f"CARRY8 cascade {chain.get('id')!r} has no native legal window"
                )
            hardblock_groups.append({
                "id": str(chain.get("id")),
                "kind": "site_cascade", "resource": "SLICE_MACRO",
                "owned_resources": ["CARRY8"],
                "instances": [names[name] for name in source_instances],
                "source_instances": source_instances,
                "window_count": window_count,
                "chain_template": {
                    "kind": "directed-site-chain/v1",
                    "chain_set": chain_set_id,
                    "chain_length": chain_length,
                    "unit_members": [
                        {"resource": resource, "z": z, "bel": bel}
                        for _cell_type, bel, resource, z in unit_signatures[0]
                    ],
                },
            })
        # The exact full-slice cascade legalizer consumes source-certified
        # CARRY_NEXT chains.  OpenPARF's rectangle-based chain legalizer cannot
        # represent native seams or holes and must not run on the same atoms.
        config["carry_chain_legalization_flag"] = 0
    slice_window_count = sum(
        len(item.get("physical_sites", {}).get("LUT", [])) == 1
        for item in coordinate_system["sites"]
    )
    for macro in slice_macros:
        if (
            macro.get("kind") == "carry8-lut6_2"
            and any(
                member.get("cell_type") == "CARRY8"
                and member.get("instance") in cascade_owned_carry
                for member in macro.get("members", [])
            )
        ):
            continue
        members = macro["members"]
        source_instances = [member["instance"] for member in members]
        if not slice_window_count:
            raise ValidationError(
                f"MUX physical macro {macro['id']!r} has no legal slice window"
            )
        hardblock_groups.append({
            "id": macro["id"],
            "kind": "site_macro",
            "resource": "SLICE_MACRO",
            "owned_resources": sorted({
                (
                    _MUX_RESOURCES[member["cell_type"]]
                    if member["cell_type"] in _MUX_RESOURCES
                    else "CARRY8" if member["cell_type"] == "CARRY8"
                    else "LUT"
                )
                for member in members
                if member["cell_type"] == "CARRY8"
                or member["cell_type"] in _MUX_RESOURCES
            }),
            "instances": [names[name] for name in source_instances],
            "source_instances": source_instances,
            "window_count": slice_window_count,
            "window_template": {
                "kind": "same-site-slice/v1",
                "site_resource": "LUT",
                "members": [
                    {
                        "resource": (
                            "LUT" if member["cell_type"] in _SLICE_LUT_TYPES
                            else "CARRY8" if member["cell_type"] == "CARRY8"
                            else _MUX_RESOURCES[member["cell_type"]]
                        ),
                        "z": _slice_role_slot(
                            member["cell_type"], member["physical_role"]
                        ),
                        "bel": member["physical_role"],
                    }
                    for member in members
                ],
            },
        })
    if typed_hardblock_mode:
        family_for_resource = {
            "DSP48E2": "DSP_CASCADE",
            "RAMB36E2": "BRAM_CASCADE",
            "URAM288": "URAM_CASCADE",
        }
        bram_candidates = _native_bram_candidates(coordinate_system, native)
        bram_by_site = {
            resource: {candidate["site"]: candidate for candidate in candidates}
            for resource, candidates in bram_candidates.items()
        }
        coordinate_sites = {}
        for item in coordinate_system["sites"]:
            for resource, site_names in item.get("physical_sites", {}).items():
                if resource not in family_for_resource or resource == "RAMB36E2":
                    continue
                for z, site_name in enumerate(site_names):
                    if site_name in coordinate_sites:
                        raise ValidationError(
                            "physical hardblock site appears in two Bookshelf tiles"
                        )
                    coordinate_sites[site_name] = {**item, "hardblock_z": z}
        native_families = {
            item.get("kind"): item
            for item in native.get("payload", {}).get("dedicated_adjacency", [])
            if isinstance(item, Mapping)
        }
        owned_hardblocks = set()
        for chain in packed.get("cascade_chains", []):
            resource = chain["cell_type"]
            if resource == "CARRY8":
                # The native carry-chain extractor/legalizer owns CARRY_NEXT;
                # site-macro groups below own each full-slice CARRY8/LUT6_2
                # unit.  Do not duplicate the same instances in the generic
                # typed hardblock legalizer.
                continue
            if resource == "RAMB18E2":
                raise ValidationError(
                    "RAMB18E2 dedicated cascades are not yet qualified"
                )
            kind = family_for_resource[resource]
            require_xilinx_native_constraint_capability(
                _native_report, "dedicated_adjacency." + kind
            )
            family = native_families.get(kind)
            if not isinstance(family, Mapping):
                raise ValidationError(f"native constraints have no {kind} family")
            instances = chain["instances"]
            overlap = owned_hardblocks.intersection(instances)
            if overlap:
                raise ValidationError(
                    f"typed hardblock cascades overlap: {sorted(overlap)!r}"
                )
            owned_hardblocks.update(instances)
            windows = []
            for physical_chain in family.get("chains", []):
                if not isinstance(physical_chain, list):
                    raise ValidationError(f"native {kind} chain is invalid")
                for start in range(0, len(physical_chain) - len(instances) + 1):
                    names_window = physical_chain[start:start + len(instances)]
                    if resource == "RAMB36E2":
                        candidates = bram_by_site[resource]
                        if any(site_name not in candidates for site_name in names_window):
                            continue
                        windows.append([
                            dict(candidates[site_name]) for site_name in names_window
                        ])
                    else:
                        if any(
                            site_name not in coordinate_sites
                            for site_name in names_window
                        ):
                            continue
                        windows.append([{
                            "site": site_name,
                            "resource": resource,
                            "x": coordinate_sites[site_name]["placement_x"],
                            "y": coordinate_sites[site_name]["placement_y"],
                            "z": coordinate_sites[site_name]["hardblock_z"],
                            "claims": _site_claim(site_name),
                        } for site_name in names_window])
            if not windows:
                raise ValidationError(
                    f"typed hardblock chain {chain.get('id')!r} has no native legal window"
                )
            hardblock_groups.append({
                "id": str(chain.get("id")), "kind": "cascade",
                "resource": resource,
                "instances": [names[name] for name in instances],
                "source_instances": list(instances), "windows": windows,
            })
        for atom in atoms:
            resource = atom["resource"]
            if resource not in _HARD_RESOURCES.values() or atom["instance"] in owned_hardblocks:
                continue
            window_set = singleton_window_sets.get(resource)
            if window_set is None:
                if resource in bram_candidates:
                    candidates = [dict(item) for item in bram_candidates[resource]]
                else:
                    candidates = [
                        {
                            **item, "site": site_name, "hardblock_z": z,
                            "claims": _site_claim(site_name),
                        }
                        for item in coordinate_system["sites"]
                        for z, site_name in enumerate(
                            item.get("physical_sites", {}).get(resource, [])
                        )
                    ]
                if not candidates:
                    raise ValidationError(
                        f"typed hardblock {resource} has no legal singleton site"
                    )
                windows = [[{
                    "site": item["site"], "resource": resource,
                    "x": (
                        item["placement_x"] if "placement_x" in item else item["x"]
                    ),
                    "y": (
                        item["placement_y"] if "placement_y" in item else item["y"]
                    ),
                    "z": (
                        item["hardblock_z"] if "hardblock_z" in item else item["z"]
                    ),
                    "claims": item["claims"],
                    **({
                        "anchor": item["anchor"], "bel": item["bel"],
                        "placement_mode": item["placement_mode"],
                        "tile": item["tile"],
                    } if resource in bram_candidates else {}),
                }] for item in candidates]
                window_set = {
                    "id": "singleton-resource:" + resource,
                    "resource": resource,
                    "windows": windows,
                }
                singleton_window_sets[resource] = window_set
            hardblock_groups.append({
                "id": "singleton:" + atom["instance"], "kind": "singleton",
                "resource": resource,
                "instances": [names[atom["instance"]]],
                "source_instances": [atom["instance"]],
                "window_set": window_set["id"],
                "window_count": len(window_set["windows"]),
            })
        expected_hardblocks = {
            atom["instance"] for atom in atoms
            if atom["resource"] in _HARD_RESOURCES.values()
        }
        covered_hardblocks = {
            instance for group in hardblock_groups
            if group["resource"] in _HARD_RESOURCES.values()
            for instance in group["source_instances"]
        }
        if covered_hardblocks != expected_hardblocks:
            raise ValidationError("typed hardblock constraints have incomplete ownership")
    site_database_path = output_dir / "site-map.sqlite3"
    site_count = _materialize_site_database(site_database_path, device_static)
    site_database_descriptor = {
        "schema": XILINX_OPENPARF_SITE_DATABASE_SCHEMA,
        "file": site_database_path.name,
        "sites": site_count,
    }
    if hardblock_groups:
        constraint_path = output_dir / "physical-macro-groups.json"
        source_identity = {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
        }
        if typed_hardblock_mode:
            source_identity.update({
                "native_constraints_sha256": _sha256(native_constraints_path),
                "provider_manifest_sha256": _sha256(provider_manifest_path),
            })
        write_json(constraint_path, {
            "schema": OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA,
            "status": "pass", "groups": hardblock_groups,
            "window_sets": [
                singleton_window_sets[resource]
                for resource in sorted(singleton_window_sets)
            ],
            "site_chain_sets": site_chain_sets,
            "source": source_identity,
            "site_database": site_database_descriptor,
        }, compact=True)
        config["typed_hardblock_chain_constraints"] = str(
            constraint_path.resolve()
        )
    # OpenPARF assigns area-type IDs by first appearance in this mapping and
    # its DataCollections currently requires FF to be area type 1.  Preserve
    # the deliberate LUT, FF, then stable hard-resource model order.
    write_json(
        output_dir / "openparf.json", config, compact=True, sort_keys=False
    )
    name_map_header = {
        "schema": OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        "top": selected_top,
        "coordinate_system": {
            "x_axis": coordinate_system["x_axis"],
            "y_axis": coordinate_system["y_axis"],
            "y_axis_order": coordinate_system["y_axis_order"],
        },
        "site_database": site_database_descriptor,
    }
    carry_cascade_chains = []
    if has_carry8:
        carry_cascade_chains = [
            chain for chain in packed.get("cascade_chains", [])
            if chain.get("cell_type") == "CARRY8"
        ]
    if hardblock_groups:
        name_map_header["physical_macro_constraints"] = {
            "schema": OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA,
            "file": "physical-macro-groups.json",
            "groups": len(hardblock_groups),
        }
    name_map = write_xilinx_openparf_atomic_name_map_storage(
        output_dir / "name_map.json",
        header=name_map_header,
        atoms=(
            {"openparf": f"a{index}", **atom}
            for index, atom in enumerate(atoms)
        ),
        carry_cascade_chains=carry_cascade_chains,
        mapped_sha256=_sha256(mapped_path),
        packed_sha256=_sha256(packed_path),
        architecture_sha256=_sha256(architecture_path),
    )
    name_map_selector = name_map.selector
    name_map_store_path = name_map.store_path
    name_map.close()
    manifest = {
        "schema": OPENPARF_ATOMIC_MANIFEST_SCHEMA,
        "status": "pass", "mode": "native-atomic-mixed-resource-qualification",
        "part": architecture.part, "atoms": len(atoms), "nets": net_count,
        "net_export": {
            "emitted": net_count,
            "dropped_single_endpoint": dropped_single_endpoint_nets,
        },
        "resources": dict(sorted(demand.items())),
        "resource_unit_capacity": {
            resource: per_site_capacity[resource]
            for resource in sorted(per_site_capacity, key=_resource_sort_key)
        },
        "placement_region": placement_region,
        "clock_region_contract": coordinate_system["clock_regions"],
        "slr_contract": coordinate_system["super_logic_regions"],
        "site_headroom_contract": device_static["site_headroom_contract"],
        "density_contract": density_contract,
        "routability_contract": {
            "provider": "openparf-rudy-pin-feedback-handoff-v12",
            "area_types": ["LUT", "FF"],
            "target_density": {
                "value": target_density,
                "minimum": _MIN_ROUTABILITY_TARGET_DENSITY,
                "maximum": _MAX_ROUTABILITY_TARGET_DENSITY,
                "utilization_headroom": _TARGET_DENSITY_HEADROOM,
            },
            "global_placement_stop_overflow": (
                _GLOBAL_PLACEMENT_STOP_OVERFLOW
            ),
            "diagnostic_legalization_handoff": {
                "maximum_logic_overflow": _DIAGNOSTIC_HANDOFF_OVERFLOW,
                "hpwl_patience": _DIAGNOSTIC_HANDOFF_PATIENCE,
                "requires_routability_adjustment": True,
                "post_adjustment_feedback_samples": (
                    _POST_ADJUSTMENT_FEEDBACK_SAMPLES
                ),
                "requires_exact_legalization_evidence": True,
                "authorizes_routing_release": False,
            },
            "adjustment_overflow_threshold": (
                _ROUTABILITY_ADJUSTMENT_OVERFLOW_THRESHOLD
            ),
            "maximum_iterations": _ROUTABILITY_ADJUSTMENT_ITERATIONS,
            "route_adjustment_exponent": _ROUTE_AREA_ADJUSTMENT_EXPONENT,
            "maximum_route_adjustment_rate": _MAX_ROUTE_AREA_ADJUSTMENT_RATE,
            "maximum_pin_adjustment_rate": _MAX_PIN_AREA_ADJUSTMENT_RATE,
            "resource_area_adjustment": False,
            "density_filler_sampling": {
                "provider": "density-bin-bounded-fillers-v2",
                "preferred_maximum_rows_per_area_type": _LOGIC_FILLER_LIMIT,
                "maximum_bin_span": [0.5, 0.5],
            },
            "sll_weighting": {
                "provider": "leaps-fixed-adam-step-v2",
                "step_size": _SLL_ADAM_STEP_SIZE,
                "delta_sll_ema_weight": _SLL_DELTA_EMA_WEIGHT,
                "accepted_range": [0.0, 1000.0],
            },
            "convergence": {
                "provider": "best-feasible-hpwl-patience-v1",
                "minimum_feasible_iterations": 96,
                "patience": 64,
                "relative_hpwl_improvement": 1.0e-4,
                "maximum_iterations_is_failure": True,
                "post_adjustment_iteration_reserve": (
                    _POST_ADJUSTMENT_ITERATION_RESERVE
                ),
            },
        },
        "runtime_validation": "unverified",
        "name_map_storage": {
            "schema": OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA,
            "logical_sha256": name_map_selector["logical_sha256"],
            "source": dict(name_map_selector["source"]),
            "atoms": name_map_selector["counts"]["atoms"],
            "carry_cascade_chains": name_map_selector["counts"][
                "carry_cascade_chains"
            ],
            "selector": {
                "file": "name_map.json",
                "bytes": (output_dir / "name_map.json").stat().st_size,
                "sha256": _sha256(output_dir / "name_map.json"),
            },
            "store": {
                "file": name_map_store_path.name,
                "bytes": name_map_store_path.stat().st_size,
                "sha256": name_map_selector["store_sha256"],
            },
        },
        "constraint_policy": {
            "ordinary_slice_clusters_are_repackable": True,
            "singleton_dsp_bram_uram_use_native_sssir_mcf": (
                not typed_hardblock_mode
            ),
            "typed_hardblock_chains_use_internal_legalizer": (
                typed_hardblock_mode
            ),
            "mux_site_macros_use_internal_legalizer": any(
                macro["kind"] != "carry8-lut6_2" for macro in slice_macros
            ),
            "carry8_site_macros_use_internal_legalizer": any(
                macro["kind"] == "carry8-lut6_2" for macro in slice_macros
            ),
            "carry8_chains_use_native_legalizer": (
                has_carry8
                and not bool(logical_carry_chains and typed_hardblock_mode)
            ),
            "carry8_chains_use_directed_site_legalizer": bool(
                logical_carry_chains and typed_hardblock_mode
            ),
            "dedicated_or_relative_constraints": (
                "native-physical-macro-legalizer"
                if hardblock_groups else "fail-closed"
            ),
            "ramb18_half_site": (
                "source-sealed-independent-half-claims"
                if typed_hardblock_mode else "fail-closed"
            ),
            "lut_policy": "8 independent 6LUT BELs; no paired 5LUT use",
        },
        "files": sorted([
            *file_names, "openparf.json", "name_map.json", "site-map.sqlite3",
            name_map_store_path.name,
            *(["physical-macro-groups.json"] if hardblock_groups else []),
        ]),
    }
    write_json(output_dir / "manifest.json", manifest, compact=True)
    atoms.close()
    return manifest


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
            certificate_builder = _AtomicPlacementCertificateBuilder(output_path)
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
                    output_path, result
                )
        return result
    finally:
        if certificate_builder is not None:
            certificate_builder.close()
        placement_index.close()


def run_xilinx_openparf_atomic_qualification(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[ArchitectureDB] = None,
    allowed_slrs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run one native SSSIR-MCF/direct-LG/ISM flow for the audited subset."""

    runtime = validate_openparf_runtime(
        install_root=openparf_install, python_executable=openparf_python
    )
    manifest = export_xilinx_openparf_atomic(
        mapped_path, packed_path, architecture_path, output_dir, top=top,
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,
        packed_value=packed_value,
        architecture=architecture,
        allowed_slrs=allowed_slrs,
    )
    placement = _reuse_openparf_result(output_dir)
    reused_openparf_result = placement is not None
    if placement is None:
        placement = run_openparf(
            output_dir / "openparf.json",
            log_path=output_dir / "openparf.log",
            install_root=openparf_install,
            python_executable=openparf_python,
            # Density feasibility belongs to analytical global placement.  The
            # production authority is the exact post-DL site/BEL/macro validator
            # below, so a bounded GP result may proceed to native legalization.
            allow_detailed_legalization_handoff=True,
        )
    ff_control_legalization = legalize_xilinx_openparf_ff_control_sets(
        placement, output_dir / "name_map.json", mapped_path,
        output_dir / "ff-control-set-legalization.json",
        mapped_value=mapped_value,
        packed_path=packed_path,
    )
    checkpoint_path = _write_openparf_result_checkpoint(output_dir, placement)
    certificate_metadata: Dict[str, Any] = {
        "ff_control_legalization": ff_control_legalization,
    }
    convergence_path = native_metrics_path(placement)
    if convergence_path.is_file():
        certificate_metadata["native_convergence"] = {
            "artifact": {
                "path": str(convergence_path),
                "bytes": convergence_path.stat().st_size,
                "sha256": _sha256(convergence_path),
            },
            "metrics": validate_openparf_native_metrics(
                convergence_path,
                allow_detailed_legalization_handoff=True,
            ),
        }
    installation = Path(str(runtime.get("installation", "")))
    python = Path(str(runtime.get("python", "")))
    if (
        (installation / "openparf.py").is_file()
        and (installation / "openparf").is_dir()
        and python.is_file()
    ):
        certificate_metadata["runtime_validation"] = "native-openparf"
    certificate = validate_xilinx_openparf_atomic_placement(
        placement, output_dir / "name_map.json", mapped_path,
        architecture_path, output_dir / "placement-certificate.json",
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,
        architecture=architecture,
        packed_path=packed_path,
        materialize_result=False,
        certificate_metadata=certificate_metadata,
    )
    return {
        "status": "pass", "runtime": runtime,
        "manifest": manifest, "placement": str(placement),
        "openparf_result": {
            "reused": reused_openparf_result,
            "checkpoint": str(checkpoint_path),
        },
        "certificate": certificate,
        "qualification_scope": (
            "native mixed-resource placement with exact MUX and CARRY8/LUT6_2 "
            "site macros, CARRY_NEXT adjacency, and typed DSP/BRAM/URAM "
            "legalization plus the rectangular multi-SLR SLL objective; "
            "clock and half-column constraints remain fail-closed"
        ),
    }


def run_xilinx_openparf_hardblock_qualification(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    native_constraints_path: Path,
    provider_manifest_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run native GP plus in-core typed DSP/BRAM/URAM legalization."""

    runtime = validate_openparf_runtime(
        install_root=openparf_install, python_executable=openparf_python
    )
    manifest = export_xilinx_openparf_atomic(
        mapped_path, packed_path, architecture_path, output_dir, top=top,
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
    )
    placement = run_openparf(
        output_dir / "openparf.json",
        log_path=output_dir / "openparf.log",
        install_root=openparf_install,
        python_executable=openparf_python,
        allow_detailed_legalization_handoff=True,
    )
    ff_control_legalization = legalize_xilinx_openparf_ff_control_sets(
        placement, output_dir / "name_map.json", mapped_path,
        output_dir / "ff-control-set-legalization.json",
        packed_path=packed_path,
    )
    certificate_metadata = {
        "ff_control_legalization": ff_control_legalization,
        "runtime_validation": "native-openparf",
    }
    convergence_path = native_metrics_path(placement)
    if convergence_path.is_file():
        certificate_metadata["native_convergence"] = {
            "artifact": {
                "path": str(convergence_path),
                "bytes": convergence_path.stat().st_size,
                "sha256": _sha256(convergence_path),
            },
            "metrics": validate_openparf_native_metrics(
                convergence_path,
                allow_detailed_legalization_handoff=True,
            ),
        }
    certificate = validate_xilinx_openparf_atomic_placement(
        placement, output_dir / "name_map.json", mapped_path,
        architecture_path, output_dir / "placement-certificate.json",
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
        packed_path=packed_path,
        materialize_result=False,
        certificate_metadata=certificate_metadata,
    )
    return {
        "status": "pass", "runtime": runtime, "manifest": manifest,
        "placement": str(placement), "certificate": certificate,
        "qualification_scope": (
            "OpenPARF global placement plus internal typed DSP/BRAM/URAM "
            "dedicated-chain legalization"
        ),
    }
