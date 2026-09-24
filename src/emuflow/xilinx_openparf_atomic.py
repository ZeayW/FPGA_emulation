"""Narrow atomic LUT/FF adapter for OpenPARF native legalization.

This is an internal qualification route, not a production placer selection.
It dissolves only ordinary, unconstrained LUT1--LUT6/FD* slice clusters and
lets OpenPARF's native direct legalizer and ISM detailed placer repack them.
Every unsupported physical relation is rejected before export.  The importer
then independently checks the conservative UltraScale+ 6LUT-only slot policy
before producing a compact EmuFlow placement certificate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ImportError, ValidationError
from .io import read_json, write_json
from .openparf import run_openparf, validate_openparf_runtime
from .xilinx_packing import (
    FF_TYPES,
    LUT_TYPES,
    PACKED_SITE_NETLIST_SCHEMA,
)


OPENPARF_ATOMIC_MANIFEST_SCHEMA = "emuflow.openparf-atomic-manifest/v1"
OPENPARF_ATOMIC_NAME_MAP_SCHEMA = "emuflow.openparf-atomic-name-map/v1"
OPENPARF_ATOMIC_PLACEMENT_SCHEMA = "emuflow.openparf-atomic-placement/v1"

_SUPPORTED = LUT_TYPES | FF_TYPES
_ALLOWED_CLUSTER_KEYS = {
    "id", "kind", "site_templates", "control_set", "assignments",
}
_ALLOWED_ASSIGNMENT_KEYS = {"instance", "cell_type", "bel"}
_FF_CLOCK = "C"
_FF_ENABLE = "CE"
_FF_SR = {"FDCE": "R", "FDRE": "R", "FDPE": "S", "FDSE": "S"}


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


def _pin_bit(cell: Mapping[str, Any], port: str) -> Any:
    connections = cell.get("connections")
    values = connections.get(port, []) if isinstance(connections, Mapping) else []
    if not isinstance(values, list) or len(values) > 1:
        raise ValidationError(f"cell port {port!r} is not a scalar pin")
    return values[0] if values else None


def _control_tuple(cell_type: str, cell: Mapping[str, Any]) -> Tuple[Any, Any, Any]:
    return (
        _pin_bit(cell, _FF_CLOCK),
        _pin_bit(cell, _FF_SR[cell_type]),
        _pin_bit(cell, _FF_ENABLE),
    )


def _slice_sites(architecture: ArchitectureDB) -> List[Dict[str, Any]]:
    sites = [
        site for site in architecture.sites
        if str(site.get("type", "")).upper().startswith("SLICE")
    ]
    if not sites:
        raise ValidationError("ArchitectureDB has no slice sites")
    by_coordinate: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    for raw_site in architecture.value["sites"]:
        by_coordinate[_physical_coordinate(raw_site)].append(raw_site["name"])
    collisions = {
        coordinate: names
        for coordinate, names in by_coordinate.items()
        if len(names) != 1
    }
    if collisions:
        raise ValidationError(
            "ArchitectureDB has coincident physical resources that the atomic "
            f"Bookshelf grid cannot distinguish: {collisions!r}"
        )
    for site in sites:
        lut_bels = {
            bel["name"] for bel in site["bels"]
            if any(cell_type in LUT_TYPES for cell_type in bel["compatible_cells"])
            and bel["name"] in {f"{letter}6LUT" for letter in "ABCDEFGH"}
        }
        ff_bels = {
            bel["name"] for bel in site["bels"]
            if any(cell_type in FF_TYPES for cell_type in bel["compatible_cells"])
            and bel["name"] in {
                name for letter in "ABCDEFGH"
                for name in (f"{letter}FF", f"{letter}FF2")
            }
        }
        if len(lut_bels) != 8 or len(ff_bels) != 16:
            raise ValidationError(
                f"slice site {site['name']!r} does not expose the required "
                "8 6LUT and 16 FF BELs"
            )
    return sites


def _collect_atoms(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any], List[Dict[str, str]]]:
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    cascades = packed.get("cascade_chains", [])
    if not isinstance(cascades, list) or cascades:
        raise ValidationError("atomic LUT/FF adapter rejects all cascade constraints")
    atoms: List[Dict[str, str]] = []
    seen = set()
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
        if cluster.get("kind") != "slice":
            raise ValidationError(
                f"atomic LUT/FF adapter rejects cluster kind {cluster.get('kind')!r}"
            )
        assignments = cluster.get("assignments")
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError(f"cluster {cluster.get('id')!r} has no assignments")
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
                    f"atomic LUT/FF adapter rejects primitive {cell_type!r}"
                )
            if instance in seen:
                raise ValidationError(f"mapped instance {instance!r} is packed twice")
            seen.add(instance)
            atoms.append({
                "instance": instance,
                "cell_type": cell_type,
                "source_cluster": str(cluster.get("id")),
            })
    physical_cells = {
        name for name, cell in cells.items()
        if isinstance(cell, Mapping) and cell.get("type") not in {"GND", "VCC"}
    }
    if seen != physical_cells:
        missing = sorted(physical_cells - seen)
        raise ValidationError(
            "atomic LUT/FF adapter requires complete physical cell coverage; "
            f"uncovered cells: {missing!r}"
        )
    counts = Counter(atom["cell_type"] in FF_TYPES for atom in atoms)
    if not counts[False] or not counts[True]:
        raise ValidationError(
            "pinned OpenPARF direct legalization requires at least one LUT and one FF"
        )
    return selected_top, cells, sorted(atoms, key=lambda item: item["instance"])


def probe_xilinx_openparf_atomic_eligibility(
    mapped: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture: ArchitectureDB,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a side-effect-free design-specific adapter decision."""

    try:
        _selected_top, _cells, atoms = _collect_atoms(mapped, packed, top)
        sites = _slice_sites(architecture)
        lut_count = sum(atom["cell_type"] in LUT_TYPES for atom in atoms)
        ff_count = len(atoms) - lut_count
        if lut_count > 8 * len(sites) or ff_count > 16 * len(sites):
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
            "ordinary LUT/FF clusters can be atomized without discarding a "
            "dedicated, relative, hard-macro, or coincident-site constraint"
        ),
        "atoms": len(atoms), "sites": len(sites),
        "resources": {"LUT": lut_count, "FF": ff_count},
    }


def _render_library(cells: Mapping[str, Any], atoms: Sequence[Mapping[str, str]]) -> str:
    ports_by_type: Dict[str, Dict[str, str]] = defaultdict(dict)
    for atom in atoms:
        cell_type = atom["cell_type"]
        cell = cells[atom["instance"]]
        directions = cell.get("port_directions")
        connections = cell.get("connections")
        if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
            raise ValidationError(f"cell {atom['instance']!r} lacks explicit port metadata")
        if set(directions) != set(connections):
            raise ValidationError(
                f"cell {atom['instance']!r} port metadata is incomplete"
            )
        for port, direction in directions.items():
            if direction not in {"input", "output"}:
                raise ValidationError(
                    f"cell {atom['instance']!r} has unsupported {direction!r} pin"
                )
            _pin_bit(cell, str(port))
            previous = ports_by_type[cell_type].setdefault(str(port), str(direction))
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
            lines.append(f"  PIN {port} {direction.upper()}{suffix}")
        lines.append("END CELL")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_nets(
    cells: Mapping[str, Any], atoms: Sequence[Mapping[str, str]], names: Mapping[str, str]
) -> Tuple[str, int]:
    endpoints: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for atom in atoms:
        instance = atom["instance"]
        cell = cells[instance]
        for port, values in cell["connections"].items():
            bit = _pin_bit(cell, str(port))
            if isinstance(bit, int) and not isinstance(bit, bool):
                endpoints[bit].append(
                    (names[instance], str(port), str(cell["port_directions"][port]))
                )
    lines = []
    for net_index, (_bit, pins) in enumerate(sorted(endpoints.items())):
        drivers = [pin for pin in pins if pin[2] == "output"]
        if len(drivers) > 1:
            raise ValidationError("mapped net has multiple atomic LUT/FF drivers")
        ordered = [*drivers, *(pin for pin in pins if pin[2] != "output")]
        lines.append(f"net n{net_index} {len(ordered)}")
        lines.extend(f"  {instance} {port}" for instance, port, _direction in ordered)
        lines.append("endnet")
    return "\n".join(lines) + "\n", len(endpoints)


def _render_sites(sites: Sequence[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    coordinates = [_physical_coordinate(site) for site in sites]
    x_axis = sorted({coordinate[0] for coordinate in coordinates})
    y_axis = sorted({coordinate[1] for coordinate in coordinates})
    x_index = {value: index for index, value in enumerate(x_axis)}
    y_index = {value: index for index, value in enumerate(y_axis)}
    lines = [
        "SITE EMUFLOW_SLICE", "  LUT 16", "  FF 16", "END SITE", "",
        "RESOURCES", "  LUT LUT1 LUT2 LUT3 LUT4 LUT5 LUT6",
        "  FF FDCE FDPE FDRE FDSE", "END RESOURCES", "",
        f"SITEMAP {len(x_axis)} {len(y_axis)}",
    ]
    site_map = []
    for site in sorted(sites, key=lambda item: _physical_coordinate(item)):
        coordinate = _physical_coordinate(site)
        dense = (x_index[coordinate[0]], y_index[coordinate[1]])
        lines.append(f"{dense[0]} {dense[1]} EMUFLOW_SLICE")
        site_map.append({
            "dense_x": dense[0], "dense_y": dense[1],
            "physical_x": coordinate[0], "physical_y": coordinate[1],
            "site": site["name"],
        })
    lines.append("END SITEMAP")
    return "\n".join(lines) + "\n", {
        "x_axis": x_axis, "y_axis": y_axis, "sites": site_map,
    }


def export_xilinx_openparf_atomic(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Export the fail-closed native LUT/FF qualification subset."""

    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    if not isinstance(packed, Mapping) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    selected_top, cells, atoms = _collect_atoms(
        mapped, packed, top if top is not None else packed.get("top")
    )
    sites = _slice_sites(architecture)
    lut_count = sum(atom["cell_type"] in LUT_TYPES for atom in atoms)
    ff_count = len(atoms) - lut_count
    if lut_count > 8 * len(sites) or ff_count > 16 * len(sites):
        raise ValidationError("atomic LUT/FF demand exceeds slice capacity")
    names = {
        atom["instance"]: f"a{index}" for index, atom in enumerate(atoms)
    }
    library = _render_library(cells, atoms)
    nets, net_count = _render_nets(cells, atoms, names)
    site_text, coordinate_system = _render_sites(sites)
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
    model_map = {
        primitive: {
            ("FF" if primitive in FF_TYPES else "LUT"): ["1", "1"],
            "isFF" if primitive in FF_TYPES else "isLUT": (
                1 if primitive in FF_TYPES else int(primitive[3:])
            ),
        }
        for primitive in sorted({atom["cell_type"] for atom in atoms})
    }
    config = {
        "benchmark_name": "xilinx_atomic_lut_ff",
        "benchmark_format": "bookshelf", "architecture_name": "ultrascale",
        "aux_input": str((output_dir / "design.aux").resolve()),
        "gpu": 0, "dtype": "float64", "target_density": 0.75,
        "random_seed": 1000, "max_global_place_iters": 2000,
        "global_place_flag": 1, "legalize_flag": 1,
        "detailed_place_flag": 1, "generic_cluster_placement_flag": 0,
        "logic_area_type_names": ["LUT", "FF"],
        "plot_flag": 0, "plot_target_at_names": ["LUT", "FF"],
        "io_at_names": [], "num_threads": 8,
        "gp_model2area_types_map": model_map,
        "gp_resource2area_types_map": {"LUT": ["LUT"], "FF": ["FF"]},
        "resource_categories": {"LUT": "LUTL", "FF": "FF"},
        "CLB_capacity": 16, "BLE_capacity": 2,
        "num_ControlSets_per_CLB": 2,
        "gp_adjust_area": 0, "gp_adjust_area_types": [],
        "gp_adjust_route_area": 0, "gp_adjust_pin_area": 0,
        "gp_adjust_resource_area": 0,
        "honor_clock_region_constraints": 0,
        "honor_half_column_constraints": 0,
        "route_flag": 0, "slr_aware_flag": 0,
        "result_dir": str((output_dir / "results").resolve()),
    }
    write_json(output_dir / "openparf.json", config, compact=True)
    write_json(output_dir / "name_map.json", {
        "schema": OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        "top": selected_top,
        "atoms": [
            {"openparf": names[atom["instance"]], **atom} for atom in atoms
        ],
        "coordinate_system": coordinate_system,
    }, compact=True)
    manifest = {
        "schema": OPENPARF_ATOMIC_MANIFEST_SCHEMA,
        "status": "pass", "mode": "native-atomic-lut-ff-qualification",
        "part": architecture.part, "atoms": len(atoms), "nets": net_count,
        "resources": {"LUT": lut_count, "FF": ff_count},
        "runtime_validation": "unverified",
        "constraint_policy": {
            "ordinary_slice_clusters_are_repackable": True,
            "dedicated_or_relative_constraints": "fail-closed",
            "lut_policy": "8 independent 6LUT BELs; no paired 5LUT use",
        },
        "files": sorted([*files, "openparf.json", "name_map.json"]),
    }
    write_json(output_dir / "manifest.json", manifest, compact=True)
    return manifest


def _slot_bel(resource: str, z: int) -> str:
    if not 0 <= z < 16:
        raise ValidationError("OpenPARF atomic placement has an invalid z slot")
    letter = "ABCDEFGH"[z // 2]
    if resource == "LUT":
        if z % 2:
            raise ValidationError(
                "OpenPARF used paired LUT packing that violates the 6LUT-only policy"
            )
        return f"{letter}6LUT"
    return f"{letter}FF" if z % 2 == 0 else f"{letter}FF2"


def validate_xilinx_openparf_atomic_placement(
    placement_path: Path,
    name_map_path: Path,
    mapped_path: Path,
    architecture_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate and aggregate native atom placement without fallback."""

    name_map = read_json(name_map_path)
    mapped = read_json(mapped_path)
    architecture = ArchitectureDB.load(architecture_path)
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
    site_map = {
        (item["dense_x"], item["dense_y"]): item["site"]
        for item in name_map.get("coordinate_system", {}).get("sites", [])
    }
    if not site_map:
        raise ValidationError("OpenPARF atomic site map is empty")
    placed: Dict[str, Dict[str, Any]] = {}
    occupied = set()
    with placement_path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.strip().split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) not in {4, 5} or fields[0] not in atoms:
                raise ImportError(
                    f"{placement_path}:{line_number}: invalid atomic placement row"
                )
            if fields[0] in placed:
                raise ValidationError("OpenPARF atomic placement duplicates an atom")
            try:
                values = [float(value) for value in fields[1:4]]
            except ValueError as error:
                raise ImportError("OpenPARF atomic placement is non-numeric") from error
            if any(not math.isfinite(value) or not value.is_integer() for value in values):
                raise ValidationError("OpenPARF atomic placement is not discrete")
            x, y, z = (int(value) for value in values)
            site_name = site_map.get((x, y))
            if site_name is None:
                raise ValidationError("OpenPARF atomic placement uses an unknown site")
            atom = atoms[fields[0]]
            cell_type = atom["cell_type"]
            resource = "FF" if cell_type in FF_TYPES else "LUT"
            bel_name = _slot_bel(resource, z)
            collision = (site_name, resource, z)
            if collision in occupied:
                raise ValidationError("OpenPARF atomic placement overlaps a BEL slot")
            occupied.add(collision)
            site = architecture.site_named(site_name)
            compatible = [
                bel for bel in site["bels"]
                if bel["name"] == bel_name
                and cell_type in bel["compatible_cells"]
            ] if site is not None else []
            if len(compatible) != 1:
                raise ValidationError(
                    "OpenPARF atomic placement has no unique compatible physical BEL"
                )
            placed[fields[0]] = {
                **atom, "site": site_name, "resource": resource, "z": z,
                "bel": bel_name,
                "placement_mode": compatible[0].get("placement_mode", site["type"]),
            }
    if set(placed) != set(atoms):
        raise ValidationError("OpenPARF atomic placement does not cover every atom")

    by_site: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in placed.values():
        by_site[item["site"]].append(item)
    for site_name, items in by_site.items():
        if sum(item["resource"] == "LUT" for item in items) > 8:
            raise ValidationError(f"site {site_name!r} exceeds physical LUT capacity")
        if sum(item["resource"] == "FF" for item in items) > 16:
            raise ValidationError(f"site {site_name!r} exceeds physical FF capacity")
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
                    "source_cluster": item["source_cluster"],
                }
                for item in sorted(items, key=lambda value: value["instance"])
            ],
        })
    result = {
        "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
        "status": "pass", "part": architecture.part,
        "provider": "openparf-native-direct-lg-ism-atomic-v1",
        "runtime_validation": "unverified",
        "clusters": clusters,
        "summary": {
            "atoms": len(placed), "occupied_sites": len(clusters),
            "luts": sum(item["resource"] == "LUT" for item in placed.values()),
            "ffs": sum(item["resource"] == "FF" for item in placed.values()),
        },
    }
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
) -> Dict[str, Any]:
    """Run native direct legalization and ISM for the audited small subset."""

    runtime = validate_openparf_runtime(
        install_root=openparf_install, python_executable=openparf_python
    )
    manifest = export_xilinx_openparf_atomic(
        mapped_path, packed_path, architecture_path, output_dir, top=top
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
            "small unconstrained LUT/FF fixture; no carry, hard macro, "
            "relative placement, clock-region, half-column, or SLR claim"
        ),
    }
