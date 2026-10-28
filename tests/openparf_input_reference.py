"""Independent small geometry and Bookshelf qualification probes."""
from __future__ import annotations
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping as MappingABC, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
import hashlib, math, os, re, sqlite3, subprocess, tempfile, threading
from emuflow.common.errors import ValidationError
from emuflow.platform.architecture import ArchitectureDB
from emuflow.common.native_tools import resolve_native_stage_command
from emuflow.physical.xilinx_netlist import _IndexedCells
from emuflow.physical.xilinx_packing import LUT_TYPES, FF_TYPES, DUAL_OUTPUT_LUT_TYPE
from tests import openparf_placement_reference as runtime
_fsync_directory = runtime._fsync_directory
import errno
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

_TARGET_DENSITY = 0.75

_QUALIFIED_LUTS_PER_SLICE = 8

_CLOCK_REGION_NAME = re.compile(r"^X([0-9]+)Y([0-9]+)$")

_MAX_CLOCKS_PER_REGION = 24

def _resource_sort_key(resource: str) -> Tuple[int, str]:
    if resource == "LUT":
        return (0, resource)
    if resource == "FF":
        return (1, resource)
    return (2, resource)


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
) -> Tuple[Dict[str, int], Dict[str, Dict[str, float]]]:
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
    result: Dict[str, Dict[str, float]] = {}
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
            immutable_sqlite_uri(self.path), uri=True
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
        with closing(self._connect()) as connection, connection:
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
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT instance,cell_type,source_cluster,resource "
                "FROM atoms WHERE seq=?", (index + 1,),
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return self._record(row)

    def ordinal_for_instance(self, instance: str) -> int:
        with closing(self._connect()) as connection, connection:
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
    nets_path: Path,
) -> Tuple[str, None, int, int]:
    """Keep typed pin traversal and net text emission entirely in C++."""

    command = resolve_native_stage_command(
        "phase7", "export-bookshelf"
    )
    database = next(
        row[2] for row in cells.connection.execute("PRAGMA database_list")
        if row[1] == "main"
    )
    parent = nets_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".emuflow-bookshelf-", dir=parent
    ) as temporary:
        root = Path(temporary)
        output = root / "design.nets"
        report = root / "summary.tsv"
        result = subprocess.run(
            [*command, str(Path(database).resolve()), str(atoms.path.resolve()),
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
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(output, nets_path)
        return library, None, emitted, dropped


def _render_library_and_nets(
    cells: _IndexedCells,
    atoms: _IndexedAtomSequence,
    names: _OrdinalSafeNames,
    *,
    platform_mux_types: Sequence[str] = (),
    nets_path: Path,
) -> Tuple[str, None, int, int]:
    """Export current typed owners through the sole native Bookshelf producer."""

    if not (
        isinstance(cells, _IndexedCells)
        and isinstance(atoms, _IndexedAtomSequence)
        and isinstance(names, _OrdinalSafeNames)
        and names.atoms is atoms
    ):
        raise ValidationError("OpenPARF Bookshelf export requires current typed owners")
    return _render_indexed_bookshelf(
        cells, atoms, platform_mux_types=platform_mux_types, nets_path=nets_path,
    )


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
    if len(sites) > 4096:
        raise ValidationError("independent geometry fixture exceeds record bound")
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
    # each logic site after one row: physical gaps are not
    # extra placement area. Hard-resource spans retain their native height.
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
        "coordinate_system": (
            "native-device-tile-column-row-v1"
            if all(isinstance(site.get("tile"), Mapping)
                   and all(isinstance(site["tile"].get(key), int)
                           and not isinstance(site["tile"][key], bool)
                           and site["tile"][key]>=0 for key in ("grid_col", "grid_row"))
                   for site, _resources in sites) else None),
        "clock_regions": clock_region_contract,
        "super_logic_regions": slr_contract,
    }



from emuflow.storage.store_publication import validate_store_destination, publish_sqlite_store, require_store_sources_unchanged
from emuflow.common.io import file_sha256
from emuflow.common.native_tools import native_file_generation
_sha256=file_sha256
XILINX_OPENPARF_SITE_DATABASE_SCHEMA=runtime.XILINX_OPENPARF_SITE_DATABASE_SCHEMA
_DEVICE_STATIC_CACHE_LOCK=threading.RLock()
_NATIVE_CARRY_Y_AXIS_ORDER_CACHE={}


def _write_site_database(
    path: Path, coordinate_system: Mapping[str, Any], *, source_bindings=()
) -> int:
    """Write the large physical site map once in a queryable representation."""
    if len(coordinate_system.get("sites", [])) > 4096:
        raise ValidationError("independent geometry fixture exceeds record bound")

    source_bindings = validate_store_destination(path, source_bindings)
    sites = coordinate_system.get("sites")
    if not isinstance(sites, list) or not sites:
        raise ValidationError("OpenPARF atomic coordinate system is empty")
    # Populating several ordered B-trees directly on shared NFS turns roughly
    # two million small inserts into minutes of random writes.  Construct the
    # database in memory, create indexes after the bulk inserts, then emit its
    # pages once through SQLite's backup API.  The final bytes retain the same
    # read-only schema and remain under the required /research run directory.
    with closing(sqlite3.connect(":memory:")) as database, database:
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
        if coordinate_system.get("coordinate_system") is not None:
            database.execute("INSERT INTO metadata VALUES ('coordinate_system',?)",
                             (coordinate_system["coordinate_system"],))
        if coordinate_system.get("architecture_sha256") is not None:
            database.execute("INSERT INTO metadata VALUES ('architecture_sha256',?)",
                             (coordinate_system["architecture_sha256"],))
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
            CREATE INDEX physical_sites_name ON physical_sites(physical_site);
        """)
        database.commit()
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as destination, destination:
                destination.executescript(
                    "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                )
                database.backup(destination, pages=4096)
            publish_sqlite_store(
                temporary, path, "openparf-site-store-v1", sources=source_bindings
            )
        finally:
            temporary.unlink(missing_ok=True)
    return len(sites)



def _materialize_site_database(
    path: Path, device_static: Dict[str, Any], *, source_bindings=()
) -> int:
    """Write one immutable device site database, then hard-link later users."""

    source_bindings = validate_store_destination(path, source_bindings)
    with _DEVICE_STATIC_CACHE_LOCK:
        source = device_static.get("site_database")
        if isinstance(source, Path) and source.is_file():
            from emuflow.common.native_tools import native_file_generation
            before = native_file_generation(source)
            expected = device_static["site_database_generation"]
            if before != expected:
                if (before[:5] != expected[:5]
                        or _sha256(source) != device_static["site_database_sha256"]
                        or native_file_generation(source) != before):
                    raise ValidationError("OpenPARF immutable device site map changed after publication")
            if path.exists() and os.path.samefile(source,path):
                device_static["site_database_generation"] = before
                return int(device_static["site_count"])
            require_store_sources_unchanged(source_bindings)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".link.tmp", dir=path.parent
            )
            os.close(descriptor)
            alias = Path(name)
            alias.unlink()
            try:
                try:
                    os.link(source, alias)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                else:
                    # The link is a namespace alias of the already sealed owner,
                    # not private SQL staging. Do not copy it to satisfy the
                    # private writer's single-link ownership requirement.
                    linked_generation = native_file_generation(source)
                    if linked_generation[:5] != before[:5]:
                        raise ValidationError(
                            "OpenPARF site map changed while creating its alias"
                        )
                    require_store_sources_unchanged(source_bindings)
                    os.replace(alias, path)
                    _fsync_directory(path.parent)
                    committed_generation = native_file_generation(source)
                    if committed_generation[:5] != before[:5]:
                        raise ValidationError(
                            "OpenPARF site map changed while publishing its alias"
                        )
                    device_static["site_database_generation"] = committed_generation
                    return int(device_static["site_count"])
            finally:
                alias.unlink(missing_ok=True)
        count = _write_site_database(
            path, device_static["coordinate_system"], source_bindings=source_bindings
        )
        # Seal this immutable device generation once, before publishing any
        # hard links. Later users reuse this seal instead of hashing per FPGA.
        from emuflow.common.native_tools import native_file_generation
        generation = native_file_generation(path)
        device_static["site_database_sha256"] = _sha256(path)
        if native_file_generation(path) != generation:
            raise ValidationError("OpenPARF device site map changed while sealing")
        device_static["site_database_generation"] = generation
        device_static["site_database"] = path
        device_static["site_count"] = count
        return count



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
