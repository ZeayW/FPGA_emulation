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
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ImportError, ValidationError
from .io import file_sha256, read_json, write_json
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


OPENPARF_ATOMIC_MANIFEST_SCHEMA = "emuflow.openparf-atomic-manifest/v1"
OPENPARF_ATOMIC_NAME_MAP_SCHEMA = "emuflow.openparf-atomic-name-map/v2"
OPENPARF_ATOMIC_PLACEMENT_SCHEMA = "emuflow.openparf-atomic-placement/v1"
OPENPARF_ATOMIC_SOURCE_SCHEMA = "emuflow.openparf-atomic-source/v1"
OPENPARF_ATOMIC_PROVIDER = (
    "openparf-native-rudy-pin-aware-mcf-direct-lg-ism-atomic-v4"
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
_FF_SR = {"FDCE": "R", "FDRE": "R", "FDPE": "S", "FDSE": "S"}
_TARGET_DENSITY = 0.75
_CLOCK_REGION_SITE_UTILIZATION_LIMIT = 0.75
_GLOBAL_PLACEMENT_STOP_OVERFLOW = 0.10
_ROUTABILITY_ADJUSTMENT_OVERFLOW_THRESHOLD = 0.15
_ROUTABILITY_ADJUSTMENT_ITERATIONS = 6
_ROUTE_AREA_ADJUSTMENT_EXPONENT = 2.0
_MAX_ROUTE_AREA_ADJUSTMENT_RATE = 2.0
_MAX_PIN_AREA_ADJUSTMENT_RATE = 1.6
_LOGIC_FILLER_LIMIT = 65_536
_CLOCK_REGION_NAME = re.compile(r"^X([0-9]+)Y([0-9]+)$")
_MAX_CLOCKS_PER_REGION = 24
_DEVICE_STATIC_CACHE_LIMIT = 4
_DEVICE_STATIC_CACHE_LOCK = threading.RLock()
_DEVICE_STATIC_CACHE: "OrderedDict[Tuple[Any, ...], Dict[str, Any]]" = (
    OrderedDict()
)
_NATIVE_CARRY_Y_AXIS_ORDER_CACHE: Dict[Tuple[Any, str], str] = {}


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
) -> List[Dict[str, Any]]:
    """Read only requested site rows from the normalized placement contract."""

    name_map = read_json(name_map_path)
    if name_map.get("schema") != expected_name_map_schema:
        raise ValidationError("OpenPARF name map is invalid")
    database_path, expected_count = _site_database_path(name_map_path, name_map)
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
) -> List[Dict[str, Any]]:
    """Read selected sites from an atomic OpenPARF placement contract."""

    return load_xilinx_openparf_sites(
        name_map_path,
        expected_name_map_schema=OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        coordinates=coordinates,
        resources=resources,
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
    return (
        _pin_bit(cell, _FF_CLOCK),
        _pin_bit(cell, _FF_SR[cell_type]),
        _pin_bit(cell, _FF_ENABLE),
    )


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


def _derate_clock_region_sites(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
) -> Tuple[
    List[Tuple[Mapping[str, Any], Mapping[str, int]]], Dict[str, Any]
]:
    """Reserve physical placement headroom before OpenPARF legalization.

    Global analytical target density is not a local clock-region occupancy
    constraint.  Expose at most 75% of each (SLR, clock region, site type) to
    every downstream legalizer.  Each physical column retains one contiguous
    run, alternating its reserved edge across columns, so dedicated vertical
    chains remain useful while the reservation is spatially distributed.
    """

    groups: Dict[
        Tuple[str, str, str],
        List[Tuple[Mapping[str, Any], Mapping[str, int]]],
    ] = defaultdict(list)
    passthrough = []
    for item in sites:
        key = _clock_region_site_key(item[0])
        if key is None:
            passthrough.append(item)
        else:
            groups[key].append(item)
    if not groups:
        return list(sites), {
            "status": "not-applicable", "limit": None,
            "groups": 0, "original_sites": len(sites),
            "available_sites": len(sites), "reserved_sites": 0,
        }

    selected_names = {item[0]["name"] for item in passthrough}
    group_available = []
    for key, group in sorted(groups.items()):
        limit = max(
            1,
            int(math.ceil(
                len(group) * _CLOCK_REGION_SITE_UTILIZATION_LIMIT
            )),
        )
        columns: Dict[int, List[Tuple[Mapping[str, Any], Mapping[str, int]]]] = (
            defaultdict(list)
        )
        for item in group:
            columns[int(item[0]["x"])].append(item)
        ordered_columns = []
        for x, column in sorted(columns.items()):
            column.sort(key=lambda item: (int(item[0]["y"]), item[0]["name"]))
            ordered_columns.append((x, column))

        allocations = [
            int(math.floor(
                len(column) * _CLOCK_REGION_SITE_UTILIZATION_LIMIT
            ))
            for _x, column in ordered_columns
        ]
        remaining = limit - sum(allocations)
        for index in range(remaining):
            allocations[index % len(allocations)] += 1

        for index, ((_x, column), count) in enumerate(
            zip(ordered_columns, allocations)
        ):
            if count <= 0:
                continue
            if len(ordered_columns) == 1:
                start = (len(column) - count) // 2
            elif index % 2:
                start = len(column) - count
            else:
                start = 0
            selected_names.update(
                item[0]["name"] for item in column[start:start + count]
            )
        available = sum(
            item[0]["name"] in selected_names for item in group
        )
        if available != limit:
            raise ValidationError(
                f"clock-region site derating failed for {key!r}: "
                f"available={available}, limit={limit}"
            )
        group_available.append(available)

    result = [item for item in sites if item[0]["name"] in selected_names]
    return result, {
        "status": "pass",
        "limit": _CLOCK_REGION_SITE_UTILIZATION_LIMIT,
        "groups": len(groups),
        "original_sites": len(sites),
        "available_sites": len(result),
        "reserved_sites": len(sites) - len(result),
        "minimum_group_available_sites": min(group_available),
        "maximum_group_available_sites": max(group_available),
    }


def _validate_native_density_contract(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
    atoms: Sequence[Mapping[str, str]],
) -> Tuple[Dict[str, int], Dict[str, Dict[str, float]]]:
    """Prove every active area type has finite density and filler headroom."""

    demand = Counter(atom["resource"] for atom in atoms)
    per_site_capacity = {
        resource: max(resources.get(resource, 0) for _site, resources in sites)
        for resource in demand
    }
    result: Dict[str, Dict[str, float]] = {}
    for resource in sorted(demand, key=_resource_sort_key):
        unit_capacity = per_site_capacity[resource]
        if unit_capacity <= 0:
            raise ValidationError(
                f"OpenPARF atomic placement has no {resource} model capacity"
            )
        available_units = sum(
            resources.get(resource, 0) for _site, resources in sites
        )
        movable_area = demand[resource] / unit_capacity
        placeable_area = available_units / unit_capacity
        target_area = _TARGET_DENSITY * placeable_area
        filler_units = available_units - demand[resource]
        if not (
            math.isfinite(movable_area)
            and math.isfinite(placeable_area)
            and 0.0 < movable_area < target_area
            and filler_units > 0
        ):
            raise ValidationError(
                "OpenPARF atomic placement density domain has no finite headroom "
                f"for {resource}: demand={demand[resource]}, "
                f"capacity={available_units}, target_density={_TARGET_DENSITY}"
            )
        result[resource] = {
            "movable_area": movable_area,
            "placeable_area": placeable_area,
            "target_area": target_area,
            "filler_units": filler_units,
        }
    return per_site_capacity, result


def _collect_atoms(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str],
    *, allow_hardblock_cascades: bool = False,
    physical_macro_contract: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, Mapping[str, Any], List[Dict[str, str]]]:
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    cascades = packed.get("cascade_chains", [])
    if not isinstance(cascades, list):
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
    atoms: List[Dict[str, str]] = []
    seen = set()
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
    if not isinstance(clusters, list) or not clusters:
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
            cell = cells.get(instance) if isinstance(instance, str) else None
            if not isinstance(cell, Mapping) or cell.get("type") != cell_type:
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
            if instance in seen:
                raise ValidationError(f"mapped instance {instance!r} is packed twice")
            seen.add(instance)
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
    physical_cells = {
        name for name, cell in cells.items()
        if isinstance(cell, Mapping) and cell.get("type") not in {"GND", "VCC"}
    }
    if seen != physical_cells:
        missing = sorted(physical_cells - seen)
        raise ValidationError(
            "atomic mixed-resource adapter requires complete physical cell coverage; "
            f"uncovered cells: {missing!r}"
        )
    counts = Counter(atom["cell_type"] in FF_TYPES for atom in atoms)
    if not counts[False] or not counts[True]:
        raise ValidationError(
            "pinned OpenPARF direct legalization requires at least one LUT and one FF"
        )
    return selected_top, cells, sorted(atoms, key=lambda item: item["instance"])


def build_xilinx_openparf_atomic_source(
    mapped_path: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
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
    clusters = []
    constants = []
    macro_owned = set()
    for macro in macro_contract.get("site_macros", []):
        assignments = []
        for member in macro["members"]:
            instance = member["instance"]
            macro_owned.add(instance)
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
        clusters.append({
            "id": macro["id"],
            "kind": "carry" if carry_macro else "hard" if ramb18_macro else "slice",
            "site_templates": (
                ["RAMB18E2"] if ramb18_macro else ["SLICEL", "SLICEM"]
            ),
            "control_set": None,
            "assignments": assignments,
        })
    for index, (name, cell) in enumerate(sorted(cells.items())):
        cell_type = cell["type"]
        if cell_type in CONSTANT_TYPES:
            constants.append(name)
            continue
        if name in macro_owned:
            continue
        hard = cell_type in _HARD_RESOURCES
        hard_bel_candidates = (
            ["RAMB18E2_L", "RAMB18E2_U"]
            if cell_type == "RAMB18E2"
            else [cell_type]
        )
        clusters.append({
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
        })
    summary = {
        "physical_atoms": len(clusters),
        "constant_cells": len(constants),
    }
    if macro_contract.get("site_macros"):
        summary["physical_macros"] = len(macro_contract["site_macros"])
    value = {
        "schema": OPENPARF_ATOMIC_SOURCE_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "source": {"mapped_sha256": _sha256(mapped_path)},
        "policy": {
            "provider": "mapped-singleton-atoms-no-site-packing-v1",
            "dedicated_or_relative_constraints": "fail-closed",
        },
        "clusters": clusters,
        "cascade_chains": cascades,
        "unplaced_constants": constants,
        "summary": summary,
    }
    write_json(output_path, value, compact=True)
    return value


def probe_xilinx_openparf_atomic_eligibility(
    mapped: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture: ArchitectureDB,
    *,
    top: Optional[str] = None,
    allow_typed_hardblocks: bool = False,
) -> Dict[str, Any]:
    """Return a side-effect-free design-specific adapter decision."""

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
    except ValidationError as error:
        return {
            "status": "adapter_required", "eligible": False,
            "adapter_validation": "missing", "reason": str(error),
        }
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


def _render_library(cells: Mapping[str, Any], atoms: Sequence[Mapping[str, str]]) -> str:
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
    y_axis = list(range(min(physical_y_values), max(physical_y_values) + 1))
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
    site_prefix = "\n".join([*lines, "RESOURCES"]) + "\n"
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
        signature = tuple(sorted(
            resources.items(), key=lambda item: _resource_sort_key(item[0])
        ))
        lines.append(f"{dense[0]} {dense[1]} {signature_names[signature]}")
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
    # OpenPARF's Bookshelf reader derives a site's bounding box from the next
    # entry in the same column (or the top of the SITEMAP for the final entry).
    # Legalizer position tensors use the center of that box, while the emitted
    # .pl file is written back using its lower-left dense coordinate.  Preserve
    # both coordinate systems explicitly so typed hard blocks do not confuse
    # the serialized site identity with the in-core legal position.
    sites_by_x: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for item in site_map:
        sites_by_x[item["dense_x"]].append(item)
    for column in sites_by_x.values():
        column.sort(key=lambda item: item["dense_y"])
        for index, item in enumerate(column):
            next_y = (
                column[index + 1]["dense_y"]
                if index + 1 < len(column)
                else len(y_axis)
            )
            item["placement_x"] = item["dense_x"] + 0.5
            item["placement_y"] = (item["dense_y"] + next_y) * 0.5
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
        width = max(entry["index"][0] for entry in by_name.values()) + 1
        height = max(entry["index"][1] for entry in by_name.values()) + 1
        expected = {(x, y) for x in range(width) for y in range(height)}
        observed = {entry["index"] for entry in by_name.values()}
        if observed != expected:
            raise ValidationError(
                "ArchitectureDB clock-region grid is incomplete"
            )
        contract_regions = []
        lines.extend(["", f"CLOCKREGIONS {width} {height}"])
        for name, entry in sorted(
            by_name.items(), key=lambda item: item[1]["index"]
        ):
            xs = [coordinate[0] for coordinate in entry["sites"]]
            ys = [coordinate[1] for coordinate in entry["sites"]]
            xl, xh = min(xs), max(xs)
            yl, yh = min(ys), max(ys)
            for item in site_map:
                if (
                    xl <= item["dense_x"] <= xh
                    and yl <= item["dense_y"] <= yh
                    and item["physical_region"]["clock_region"] != name
                ):
                    raise ValidationError(
                        f"ArchitectureDB clock region {name!r} is not rectangular"
                    )
            ymid = yl + (yh - yl + 1) // 2
            lines.append(
                f"  CLOCKREGION {name} : {xl} {yl} {xh} {yh} {ymid} {xl}"
            )
            contract_regions.append({
                "name": name, "x": entry["index"][0],
                "y": entry["index"][1], "slr": entry["slr"],
                "bbox": [xl, yl, xh, yh],
            })
        lines.append("END CLOCKREGIONS")
        clock_region_contract = {
            "width": width, "height": height,
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
    return site_prefix, "\n".join(lines) + "\n", {
        "x_axis": x_axis, "y_axis": y_axis,
        "y_axis_order": y_axis_order, "sites": site_map,
        "clock_regions": clock_region_contract,
    }


def _render_sites(
    device_static: Mapping[str, Any],
    atoms: Sequence[Mapping[str, str]],
) -> str:
    """Add partition-specific model declarations to shared device geometry."""

    models_by_resource: Dict[str, set[str]] = defaultdict(set)
    for atom in atoms:
        models_by_resource[atom["resource"]].add(atom["cell_type"])
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
) -> Dict[str, Any]:
    """Build immutable device geometry once across parallel FPGA workers."""

    y_axis_order = _native_carry_y_axis_order(architecture, native)
    key = (
        architecture,
        tuple(sorted(hard_resources)),
        tuple(sorted(mux_resources)),
        bool(require_carry8),
        y_axis_order,
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
        placement_region = _validate_native_placement_region(sites)
        sites, site_headroom_contract = _derate_clock_region_sites(sites)
        placement_region = {
            **placement_region,
            "available_logic_sites": sum(
                "LUT" in resources and "FF" in resources
                for _site, resources in sites
            ),
        }
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
    architecture: Optional[ArchitectureDB] = None,
) -> Dict[str, Any]:
    """Export the fail-closed native mixed-resource qualification subset."""

    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    packed = read_json(packed_path)
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
    mux = sorted({
        atom["cell_type"] for atom in atoms
        if atom["cell_type"] in _MUX_RESOURCES
    })
    has_carry8 = any(atom["cell_type"] == "CARRY8" for atom in atoms)
    device_static = _device_static_geometry(
        architecture, hard, mux, require_carry8=has_carry8, native=native
    )
    sites = device_static["sites"]
    placement_region = device_static["placement_region"]
    per_site_capacity, density_contract = _validate_native_density_contract(
        sites, atoms
    )
    demand = Counter(atom["resource"] for atom in atoms)
    capacity = Counter()
    for _site, resources in sites:
        capacity.update(resources)
    # The native CLB model exposes 16 LUT resource units for two-LUT BLE
    # compatibility, but EmuFlow's conservative architecture policy permits
    # only eight independent 6LUT BELs.  The importer enforces even z slots.
    capacity["LUT"] = 8 * sum("LUT" in resources for _site, resources in sites)
    for resource, count in demand.items():
        if count > capacity[resource]:
            raise ValidationError(
                f"atomic demand {count} exceeds {resource} capacity {capacity[resource]}"
            )
    names = {
        atom["instance"]: f"a{index}" for index, atom in enumerate(atoms)
    }
    library = _render_library(cells, atoms)
    nets, net_count, dropped_single_endpoint_nets = _render_nets(
        cells, atoms, names
    )
    coordinate_system = device_static["coordinate_system"]
    site_text = _render_sites(device_static, atoms)
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "design.nodes": "".join(
            f"{names[atom['instance']]} {atom['cell_type']}\n" for atom in atoms
        ),
        "design.lib": library,
        "design.nets": nets,
        "design.scl": site_text,
        "design.pl": "",
        "design.aux": "design : design.nodes design.nets design.pl design.scl design.lib\n",
    }
    for name, text in files.items():
        (output_dir / name).write_text(text, encoding="utf-8")
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
        "gpu": 0, "dtype": "float64", "target_density": _TARGET_DENSITY,
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
        "global_place_flag": 1, "legalize_flag": 1,
        "detailed_place_flag": 1, "generic_cluster_placement_flag": 0,
        "logic_area_type_names": ["LUT", "FF"],
        "plot_flag": 0,
        "plot_target_at_names": sorted(demand, key=_resource_sort_key),
        "io_at_names": [], "num_threads": 8,
        "gp_model2area_types_map": model_map,
        # Sixteen capacity-normalized LUT/FF slots share one physical slice.
        # Median-cell fillers would therefore materialize roughly sixteen
        # Python/Torch rows per unused site (millions on XCVU19P).  Sixteen
        # samples per density bin are already finer than the analytical grid;
        # preserve exact filler area while bounding the tensor population.
        "gp_max_fillers_per_area_type": {
            resource: (
                _LOGIC_FILLER_LIMIT if resource in {"LUT", "FF"} else 1
            )
            for resource in sorted(demand, key=_resource_sort_key)
        },
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
        "route_flag": 0, "slr_aware_flag": 0,
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
                "clock-region site derating removed every CARRY_NEXT window"
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
    name_map = {
        "schema": OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        "top": selected_top,
        "atoms": [
            {"openparf": names[atom["instance"]], **atom} for atom in atoms
        ],
        "coordinate_system": {
            "x_axis": coordinate_system["x_axis"],
            "y_axis": coordinate_system["y_axis"],
            "y_axis_order": coordinate_system["y_axis_order"],
        },
        "site_database": site_database_descriptor,
    }
    if has_carry8:
        name_map["carry_cascade_chains"] = [
            chain for chain in packed.get("cascade_chains", [])
            if chain.get("cell_type") == "CARRY8"
        ]
    if hardblock_groups:
        name_map["physical_macro_constraints"] = {
            "schema": OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA,
            "file": "physical-macro-groups.json",
            "groups": len(hardblock_groups),
        }
    write_json(output_dir / "name_map.json", name_map, compact=True)
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
        "site_headroom_contract": device_static["site_headroom_contract"],
        "density_contract": density_contract,
        "routability_contract": {
            "provider": "openparf-rudy-pin-stable-feasible-v3",
            "area_types": ["LUT", "FF"],
            "global_placement_stop_overflow": (
                _GLOBAL_PLACEMENT_STOP_OVERFLOW
            ),
            "adjustment_overflow_threshold": (
                _ROUTABILITY_ADJUSTMENT_OVERFLOW_THRESHOLD
            ),
            "maximum_iterations": _ROUTABILITY_ADJUSTMENT_ITERATIONS,
            "route_adjustment_exponent": _ROUTE_AREA_ADJUSTMENT_EXPONENT,
            "maximum_route_adjustment_rate": _MAX_ROUTE_AREA_ADJUSTMENT_RATE,
            "maximum_pin_adjustment_rate": _MAX_PIN_AREA_ADJUSTMENT_RATE,
            "resource_area_adjustment": False,
            "convergence": {
                "provider": "best-feasible-hpwl-patience-v1",
                "minimum_feasible_iterations": 96,
                "patience": 64,
                "relative_hpwl_improvement": 1.0e-4,
                "maximum_iterations_is_failure": True,
            },
        },
        "runtime_validation": "unverified",
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
            *files, "openparf.json", "name_map.json", "site-map.sqlite3",
            *(["physical-macro-groups.json"] if hardblock_groups else []),
        ]),
    }
    write_json(output_dir / "manifest.json", manifest, compact=True)
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
) -> Dict[str, Any]:
    """Validate and aggregate native atom placement without fallback."""

    name_map = read_json(name_map_path)
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    if name_map.get("schema") != OPENPARF_ATOMIC_NAME_MAP_SCHEMA:
        raise ValidationError("OpenPARF atomic name map is invalid")
    _top, module = _select_module(mapped, name_map.get("top"))
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    atoms = {
        item["openparf"]: item for item in name_map.get("atoms", [])
        if isinstance(item, Mapping) and isinstance(item.get("openparf"), str)
    }
    if len(atoms) != len(name_map.get("atoms", [])) or not atoms:
        raise ValidationError("OpenPARF atomic name map has duplicate atoms")
    raw_placement: Dict[str, Tuple[int, int, int]] = {}
    with placement_path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.strip().split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) not in {4, 5} or fields[0] not in atoms:
                raise ImportError(
                    f"{placement_path}:{line_number}: invalid atomic placement row"
                )
            if fields[0] in raw_placement:
                raise ValidationError("OpenPARF atomic placement duplicates an atom")
            try:
                values = [float(value) for value in fields[1:4]]
            except ValueError as error:
                raise ImportError("OpenPARF atomic placement is non-numeric") from error
            if any(
                not math.isfinite(value) or not value.is_integer()
                for value in values
            ):
                raise ValidationError("OpenPARF atomic placement is not discrete")
            raw_placement[fields[0]] = tuple(int(value) for value in values)
    if set(raw_placement) != set(atoms):
        raise ValidationError("OpenPARF atomic placement does not cover every atom")
    requested_coordinates = sorted({
        (x, y) for x, y, _z in raw_placement.values()
    })
    site_map = {
        (item["dense_x"], item["dense_y"]): item
        for item in load_xilinx_openparf_atomic_sites(
            name_map_path, coordinates=requested_coordinates
        )
    }
    hardblock_groups = _load_physical_macro_groups(name_map_path, name_map)
    carry_cascade_chains = name_map.get("carry_cascade_chains", [])
    if not isinstance(carry_cascade_chains, list) or any(
        not isinstance(chain, Mapping)
        or chain.get("cell_type") != "CARRY8"
        or not isinstance(chain.get("instances"), list)
        or len(chain["instances"]) < 2
        for chain in carry_cascade_chains
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
            name_map_path, resources=("RAMB18E2", "RAMB36E2")
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
    placed: Dict[str, Dict[str, Any]] = {}
    occupied = set()
    for openparf_name, (x, y, z) in raw_placement.items():
            site_entry = site_map.get((x, y))
            if site_entry is None:
                raise ValidationError("OpenPARF atomic placement uses an unknown site")
            atom = atoms[openparf_name]
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
            collision = (site_name, resource, z)
            if collision in occupied:
                raise ValidationError("OpenPARF atomic placement overlaps a BEL slot")
            occupied.add(collision)
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
            placed[openparf_name] = {
                **atom, "site": site_name, "resource": resource, "z": z,
                "x": site_entry["placement_x"],
                "y": site_entry["placement_y"],
                "bel": physical_bel["name"],
                "placement_mode": (
                    candidate["placement_mode"]
                    if resource in {"RAMB18E2", "RAMB36E2"} and bram_view_by_slot
                    else physical_bel.get("placement_mode", site["type"])
                ),
                "anchor_site": anchor_name,
            }

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
        by_source_instance = {
            item["instance"]: item for item in placed.values()
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
                selected = [by_source_instance[name] for name in instances]
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
                mapped_path, top=name_map.get("top")
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
        expected = {
            item["instance"] for item in placed.values()
            if item["resource"] in _HARD_RESOURCES.values()
        } | expected_mux_members | expected_carry_members
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
        by_instance = {item["instance"]: item for item in placed.values()}
        for chain in carry_cascade_chains:
            instances = chain["instances"]
            try:
                sites = [by_instance[name]["site"] for name in instances]
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

    by_site: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in placed.values():
        by_site[item["anchor_site"]].append(item)
    for site_name, items in by_site.items():
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

    clusters = []
    for site_name, items in sorted(by_site.items()):
        site = architecture.site_named(site_name)
        clusters.append({
            "cluster": f"openparf:{site_name}", "site": site_name,
            "site_type": site["type"], "x": site["x"], "y": site["y"],
            "assignments": [
                {
                    "instance": item["instance"],
                    "cell_type": item["cell_type"], "bel": item["bel"],
                    "placement_mode": item["placement_mode"],
                    "physical_site": item["site"],
                    "source_cluster": item["source_cluster"],
                }
                for item in sorted(items, key=lambda value: value["instance"])
            ],
        })
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
        "clusters": clusters,
        "summary": {
            "atoms": len(placed), "occupied_sites": len(clusters),
            "luts": sum(item["resource"] == "LUT" for item in placed.values()),
            "ffs": sum(item["resource"] == "FF" for item in placed.values()),
            "hard_resources": dict(sorted(Counter(
                item["resource"] for item in placed.values()
                if item["resource"] in _HARD_RESOURCES.values()
            ).items())),
        },
    }
    carry8_macro_count = sum(
        item["resource"] == "CARRY8" for item in placed.values()
    )
    if carry8_macro_count:
        result["summary"]["carry8_macros"] = carry8_macro_count
    if checked_carry_edges:
        result["summary"]["native_carry_edges"] = checked_carry_edges
    if hardblock_groups:
        if native_hardblock_groups or carry_cascade_chains:
            assert native_constraints_path is not None
            assert provider_manifest_path is not None
            result["source"].update({
                "native_constraints_sha256": _sha256(native_constraints_path),
                "provider_manifest_sha256": _sha256(provider_manifest_path),
            })
        if checked_native_edges:
            result["summary"]["native_hardblock_edges"] = checked_native_edges
    if output_path is not None:
        write_json(output_path, result, compact=True)
    return result


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
    architecture: Optional[ArchitectureDB] = None,
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
        architecture=architecture,
    )
    placement = run_openparf(
        output_dir / "openparf.json",
        log_path=output_dir / "openparf.log",
        install_root=openparf_install,
        python_executable=openparf_python,
    )
    certificate = validate_xilinx_openparf_atomic_placement(
        placement, output_dir / "name_map.json", mapped_path,
        architecture_path, output_dir / "placement-certificate.json",
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,
        architecture=architecture,
    )
    convergence_path = native_metrics_path(placement)
    if convergence_path.is_file():
        certificate["native_convergence"] = {
            "artifact": {
                "path": str(convergence_path),
                "bytes": convergence_path.stat().st_size,
                "sha256": _sha256(convergence_path),
            },
            "metrics": validate_openparf_native_metrics(convergence_path),
        }
        write_json(
            output_dir / "placement-certificate.json", certificate, compact=True
        )
    installation = Path(str(runtime.get("installation", "")))
    python = Path(str(runtime.get("python", "")))
    if (
        (installation / "openparf.py").is_file()
        and (installation / "openparf").is_dir()
        and python.is_file()
    ):
        certificate["runtime_validation"] = "native-openparf"
        write_json(
            output_dir / "placement-certificate.json", certificate, compact=True
        )
    return {
        "status": "pass", "runtime": runtime,
        "manifest": manifest, "placement": str(placement),
        "certificate": certificate,
        "qualification_scope": (
            "native mixed-resource placement with exact MUX and CARRY8/LUT6_2 "
            "site macros, CARRY_NEXT adjacency, and typed DSP/BRAM/URAM "
            "legalization; clock and SLR constraints remain fail-closed"
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
    )
    certificate = validate_xilinx_openparf_atomic_placement(
        placement, output_dir / "name_map.json", mapped_path,
        architecture_path, output_dir / "placement-certificate.json",
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
    )
    convergence_path = native_metrics_path(placement)
    if convergence_path.is_file():
        certificate["native_convergence"] = {
            "artifact": {
                "path": str(convergence_path),
                "bytes": convergence_path.stat().st_size,
                "sha256": _sha256(convergence_path),
            },
            "metrics": validate_openparf_native_metrics(convergence_path),
        }
    certificate["runtime_validation"] = "native-openparf"
    write_json(output_dir / "placement-certificate.json", certificate, compact=True)
    return {
        "status": "pass", "runtime": runtime, "manifest": manifest,
        "placement": str(placement), "certificate": certificate,
        "qualification_scope": (
            "OpenPARF global placement plus internal typed DSP/BRAM/URAM "
            "dedicated-chain legalization"
        ),
    }
