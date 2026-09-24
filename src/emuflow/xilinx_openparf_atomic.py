"""Narrow atomic LUT/FF plus singleton-hard OpenPARF native adapter.

This is an internal qualification route, not a production placer selection.
It dissolves only ordinary, unconstrained LUT1--LUT6/FD* slice clusters and
lets OpenPARF's native direct legalizer and ISM detailed placer repack them.
Independent singleton DSP48E2/RAMB36E2/URAM288 objects use the same run's
native single-site-resource min-cost-flow legalization.
Every unsupported physical relation is rejected before export.  The importer
then independently checks the conservative UltraScale+ 6LUT-only slot policy
before producing a compact EmuFlow placement certificate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
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

_HARD_RESOURCES = {
    "DSP48E2": "DSP48E2",
    "RAMB36E2": "RAMB36E2",
    "URAM288": "URAM288",
}
_SUPPORTED = LUT_TYPES | FF_TYPES | set(_HARD_RESOURCES)
_ALLOWED_CLUSTER_KEYS = {
    "id", "kind", "site_templates", "control_set", "assignments",
}
_ALLOWED_ASSIGNMENT_KEYS = {
    "instance", "cell_type", "bel", "bel_candidates",
}
_FF_CLOCK = "C"
_FF_ENABLE = "CE"
_FF_SR = {"FDCE": "R", "FDRE": "R", "FDPE": "S", "FDSE": "S"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    architecture: ArchitectureDB, hard_resources: Sequence[str]
) -> List[Tuple[Dict[str, Any], Dict[str, int]]]:
    slice_sites = [
        site for site in architecture.sites
        if str(site.get("type", "")).upper().startswith("SLICE")
    ]
    if not slice_sites:
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
    for site in slice_sites:
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
    result = [(site, {"LUT": 16, "FF": 16}) for site in slice_sites]
    hard_capacity = Counter()
    for site in architecture.sites:
        if str(site.get("type", "")).upper().startswith("SLICE"):
            continue
        matches = [
            primitive for primitive in hard_resources
            if sum(
                primitive in bel["compatible_cells"] for bel in site["bels"]
            ) == 1
        ]
        if len(matches) > 1:
            raise ValidationError(
                f"site {site['name']!r} is ambiguous for demanded hard resources"
            )
        if matches:
            result.append((site, {_HARD_RESOURCES[matches[0]]: 1}))
            hard_capacity[matches[0]] += 1
    missing = sorted(set(hard_resources) - set(hard_capacity))
    if missing:
        raise ValidationError(
            f"ArchitectureDB has no unique sites for hard resources {missing!r}"
        )
    return result


def _collect_atoms(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any], List[Dict[str, str]]]:
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    cascades = packed.get("cascade_chains", [])
    if not isinstance(cascades, list) or cascades:
        raise ValidationError("atomic mixed-resource adapter rejects all cascade constraints")
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
        kind = cluster.get("kind")
        if kind not in {"slice", "hard"}:
            raise ValidationError(
                f"atomic mixed-resource adapter rejects cluster kind {kind!r}"
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
                    f"atomic mixed-resource adapter rejects primitive {cell_type!r}"
                )
            if kind == "slice" and cell_type not in LUT_TYPES | FF_TYPES:
                raise ValidationError(
                    f"slice cluster contains hard primitive {cell_type!r}"
                )
            if kind == "hard" and (
                cell_type not in _HARD_RESOURCES or len(assignments) != 1
            ):
                raise ValidationError(
                    "hard-resource support is limited to independent singleton "
                    "DSP48E2, RAMB36E2, or URAM288 clusters"
                )
            if instance in seen:
                raise ValidationError(f"mapped instance {instance!r} is packed twice")
            seen.add(instance)
            atoms.append({
                "instance": instance,
                "cell_type": cell_type,
                "source_cluster": str(cluster.get("id")),
                "resource": (
                    "FF" if cell_type in FF_TYPES
                    else "LUT" if cell_type in LUT_TYPES
                    else _HARD_RESOURCES[cell_type]
                ),
            })
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
        hard = sorted({
            atom["cell_type"] for atom in atoms
            if atom["cell_type"] in _HARD_RESOURCES
        })
        sites = _placement_sites(architecture, hard)
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


def _render_sites(
    sites: Sequence[Tuple[Mapping[str, Any], Mapping[str, int]]],
    atoms: Sequence[Mapping[str, str]],
) -> Tuple[str, Dict[str, Any]]:
    coordinates = [_physical_coordinate(site) for site, _resources in sites]
    x_axis = sorted({coordinate[0] for coordinate in coordinates})
    y_axis = sorted({coordinate[1] for coordinate in coordinates})
    x_index = {value: index for index, value in enumerate(x_axis)}
    y_index = {value: index for index, value in enumerate(y_axis)}
    signatures = sorted({
        tuple(sorted(resources.items(), key=lambda item: _resource_sort_key(item[0])))
        for _site, resources in sites
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
    models_by_resource: Dict[str, set[str]] = defaultdict(set)
    for atom in atoms:
        models_by_resource[atom["resource"]].add(atom["cell_type"])
    lines.append("RESOURCES")
    for resource, models in sorted(
        models_by_resource.items(), key=lambda item: _resource_sort_key(item[0])
    ):
        lines.append(f"  {resource} {' '.join(sorted(models))}")
    lines.extend([
        "END RESOURCES", "", f"SITEMAP {len(x_axis)} {len(y_axis)}",
    ])
    site_map = []
    for site, resources in sorted(
        sites, key=lambda item: _physical_coordinate(item[0])
    ):
        coordinate = _physical_coordinate(site)
        dense = (x_index[coordinate[0]], y_index[coordinate[1]])
        signature = tuple(sorted(
            resources.items(), key=lambda item: _resource_sort_key(item[0])
        ))
        lines.append(f"{dense[0]} {dense[1]} {signature_names[signature]}")
        site_map.append({
            "dense_x": dense[0], "dense_y": dense[1],
            "physical_x": coordinate[0], "physical_y": coordinate[1],
            "site": site["name"],
            "resources": dict(sorted(resources.items())),
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
    """Export the fail-closed native mixed-resource qualification subset."""

    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    if not isinstance(packed, Mapping) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    selected_top, cells, atoms = _collect_atoms(
        mapped, packed, top if top is not None else packed.get("top")
    )
    hard = sorted({
        atom["cell_type"] for atom in atoms
        if atom["cell_type"] in _HARD_RESOURCES
    })
    sites = _placement_sites(architecture, hard)
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
    site_text, coordinate_system = _render_sites(sites, atoms)
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
    per_site_capacity = {
        resource: max(
            resources.get(resource, 0) for _site, resources in sites
        )
        for resource in demand
    }
    model_map = {}
    for primitive in sorted(
        {atom["cell_type"] for atom in atoms}, key=_primitive_sort_key
    ):
        resource = (
            "LUT" if primitive in LUT_TYPES
            else "FF" if primitive in FF_TYPES
            else _HARD_RESOURCES[primitive]
        )
        unit_dimension = 1.0 / math.sqrt(per_site_capacity[resource])
        if primitive in FF_TYPES:
            model_map[primitive] = {
                "FF": [unit_dimension, unit_dimension], "isFF": 1,
            }
        elif primitive in LUT_TYPES:
            model_map[primitive] = {
                "LUT": [unit_dimension, unit_dimension],
                "isLUT": int(primitive[3:]),
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
            else "SSSIR"
        )
        for resource in sorted(demand, key=_resource_sort_key)
    }
    config = {
        "benchmark_name": "xilinx_atomic_mixed_resource",
        "benchmark_format": "bookshelf", "architecture_name": "ultrascale",
        "aux_input": str((output_dir / "design.aux").resolve()),
        "gpu": 0, "dtype": "float64", "target_density": 0.75,
        "random_seed": 1000, "max_global_place_iters": 2000,
        "global_place_flag": 1, "legalize_flag": 1,
        "detailed_place_flag": 1, "generic_cluster_placement_flag": 0,
        "logic_area_type_names": ["LUT", "FF"],
        "plot_flag": 0,
        "plot_target_at_names": sorted(demand, key=_resource_sort_key),
        "io_at_names": [], "num_threads": 8,
        "gp_model2area_types_map": model_map,
        "gp_resource2area_types_map": resource_map,
        "resource_categories": resource_categories,
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
    # OpenPARF assigns area-type IDs by first appearance in this mapping and
    # its DataCollections currently requires FF to be area type 1.  Preserve
    # the deliberate LUT, FF, then stable hard-resource model order.
    write_json(
        output_dir / "openparf.json", config, compact=True, sort_keys=False
    )
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
        "runtime_validation": "unverified",
        "constraint_policy": {
            "ordinary_slice_clusters_are_repackable": True,
            "singleton_dsp_bram_uram_use_native_sssir_mcf": True,
            "dedicated_or_relative_constraints": "fail-closed",
            "ramb18_half_site": "fail-closed",
            "lut_policy": "8 independent 6LUT BELs; no paired 5LUT use",
        },
        "files": sorted([*files, "openparf.json", "name_map.json"]),
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
        if cell_type not in LUT_TYPES:
            raise ValidationError("OpenPARF LUT slot contains a non-LUT primitive")
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
        (item["dense_x"], item["dense_y"]): item
        for item in name_map.get("coordinate_system", {}).get("sites", [])
        if isinstance(item, Mapping)
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
            site_entry = site_map.get((x, y))
            if site_entry is None:
                raise ValidationError("OpenPARF atomic placement uses an unknown site")
            site_name = site_entry["site"]
            atom = atoms[fields[0]]
            cell_type = atom["cell_type"]
            resource = atom.get("resource")
            resources = site_entry.get("resources")
            if (
                not isinstance(resources, Mapping)
                or (
                    resource not in {"LUT", "FF"}
                    and resources.get(resource, 0) != 1
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
            elif resource in _HARD_RESOURCES.values():
                if z != 0:
                    raise ValidationError(
                        "OpenPARF singleton hard-resource placement must use z=0"
                    )
                bel_name = None
            else:
                raise ValidationError("OpenPARF atomic placement has an unknown resource")
            collision = (site_name, resource, z)
            if collision in occupied:
                raise ValidationError("OpenPARF atomic placement overlaps a BEL slot")
            occupied.add(collision)
            site = architecture.site_named(site_name)
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
            placed[fields[0]] = {
                **atom, "site": site_name, "resource": resource, "z": z,
                "bel": physical_bel["name"],
                "placement_mode": physical_bel.get("placement_mode", site["type"]),
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
        hard_items = [
            item for item in items if item["resource"] in _HARD_RESOURCES.values()
        ]
        if len(hard_items) > 1:
            raise ValidationError(
                f"site {site_name!r} has multiple singleton hard resources"
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
                    "source_cluster": item["source_cluster"],
                }
                for item in sorted(items, key=lambda value: value["instance"])
            ],
        })
    result = {
        "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
        "status": "pass", "part": architecture.part,
        "provider": "openparf-native-mcf-direct-lg-ism-atomic-v1",
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
    """Run one native SSSIR-MCF/direct-LG/ISM flow for the audited subset."""

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
            "small unconstrained LUT/FF plus independent singleton "
            "DSP48E2/RAMB36E2/URAM288 fixture; no carry, RAMB18 half-site, "
            "cascade, relative placement, clock-region, half-column, or SLR claim"
        ),
    }
