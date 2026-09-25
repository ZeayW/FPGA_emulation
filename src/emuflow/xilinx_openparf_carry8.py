"""Native OpenPARF CARRY8/LUT6_2 qualification route.

This is an explicit macro route, not an extension selected implicitly by the
atomic adapter.  It exports unplaced CARRY8 and LUT6_2 instances, invokes the
native chain extractor/legalizer, and independently verifies full-slice macro
ownership plus RapidWright-certified CARRY_NEXT adjacency.
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
from .xilinx_native_device_constraints import (
    load_xilinx_native_device_constraints,
    require_xilinx_native_constraint_capability,
)
from .xilinx_openparf_atomic import (
    _expanded_pins,
    _physical_coordinate,
    _render_nets,
    _render_sites,
    _resource_sort_key,
    _select_module,
    _sha256,
    _validate_native_density_contract,
    _validate_native_placement_region,
)
from .xilinx_packing import (
    CONSTANT_TYPES,
    DUAL_OUTPUT_LUT_TYPE,
    FF_TYPES,
    LUT_TYPES,
    PACKED_SITE_NETLIST_SCHEMA,
    validate_xilinx_packing,
)


OPENPARF_CARRY8_MANIFEST_SCHEMA = "emuflow.openparf-carry8-manifest/v1"
OPENPARF_CARRY8_NAME_MAP_SCHEMA = "emuflow.openparf-carry8-name-map/v1"
OPENPARF_CARRY8_PLACEMENT_SCHEMA = "emuflow.openparf-carry8-placement/v1"
OPENPARF_CARRY8_PROVIDER = "openparf-native-carry8-full-slice-v1"
_TARGET_DENSITY = 0.75
_SLICE_LUT_TYPES = LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE}


def _slice_sites(
    architecture: ArchitectureDB,
) -> List[Tuple[Mapping[str, Any], Dict[str, int]]]:
    sites = []
    for site in architecture.sites:
        if not str(site.get("type", "")).upper().startswith("SLICE"):
            continue
        bels = site.get("bels", [])
        lut_bels = {
            bel.get("name") for bel in bels
            if bel.get("name") in {f"{letter}6LUT" for letter in "ABCDEFGH"}
            and DUAL_OUTPUT_LUT_TYPE in bel.get("compatible_cells", [])
        }
        ff_bels = {
            bel.get("name") for bel in bels
            if bel.get("name") in {
                name for letter in "ABCDEFGH"
                for name in (f"{letter}FF", f"{letter}FF2")
            }
            and any(cell_type in FF_TYPES for cell_type in bel.get("compatible_cells", []))
        }
        carry_bels = [
            bel for bel in bels
            if bel.get("name") == "CARRY8"
            and "CARRY8" in bel.get("compatible_cells", [])
        ]
        if len(lut_bels) != 8 or len(ff_bels) != 16 or len(carry_bels) != 1:
            raise ValidationError(
                f"slice site {site.get('name')!r} lacks the complete "
                "LUT6_2/FF/CARRY8 physical model"
            )
        sites.append((site, {"LUT": 16, "FF": 16, "CARRY8": 1}))
    if not sites:
        raise ValidationError("ArchitectureDB has no CARRY8-capable slice sites")
    coordinates = [_physical_coordinate(site) for site, _resources in sites]
    if len(coordinates) != len(set(coordinates)):
        raise ValidationError("ArchitectureDB has coincident CARRY8 slice sites")
    return sites


def _collect(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any], List[Dict[str, str]], List[Dict[str, Any]]]:
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    clusters = packed.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError("PackedSiteNetlist clusters are invalid")
    atoms: List[Dict[str, str]] = []
    carry_macros: List[Dict[str, Any]] = []
    owners = set()
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            raise ValidationError("PackedSiteNetlist cluster is invalid")
        kind = cluster.get("kind")
        if kind not in {"slice", "carry"}:
            raise ValidationError(
                "CARRY8 native qualification currently accepts only ordinary "
                "slice atoms and CARRY8 full-slice macros"
            )
        assignments = cluster.get("assignments")
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError("PackedSiteNetlist cluster has no assignments")
        macro_members = []
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                raise ValidationError("packed assignment is invalid")
            name = assignment.get("instance")
            cell_type = assignment.get("cell_type")
            cell = cells.get(name) if isinstance(name, str) else None
            if not isinstance(cell, Mapping) or cell.get("type") != cell_type:
                raise ValidationError("packed assignment does not match mapped JSON")
            if name in owners:
                raise ValidationError(f"mapped instance {name!r} is packed twice")
            owners.add(name)
            if kind == "slice":
                if cell_type not in LUT_TYPES | FF_TYPES:
                    raise ValidationError(
                        f"ordinary slice contains unsupported primitive {cell_type!r}"
                    )
                resource = "FF" if cell_type in FF_TYPES else "LUT"
            else:
                if cell_type not in {"CARRY8", DUAL_OUTPUT_LUT_TYPE}:
                    raise ValidationError(
                        f"carry macro contains unsupported primitive {cell_type!r}"
                    )
                resource = "CARRY8" if cell_type == "CARRY8" else "LUT"
                macro_members.append({
                    "instance": name,
                    "cell_type": cell_type,
                    "bel": assignment.get("bel"),
                })
            atoms.append({
                "instance": name,
                "cell_type": str(cell_type),
                "source_cluster": str(cluster.get("id")),
                "resource": resource,
            })
        if kind == "carry":
            counts = Counter(member["cell_type"] for member in macro_members)
            bels = {member["bel"] for member in macro_members}
            if counts != Counter({"CARRY8": 1, DUAL_OUTPUT_LUT_TYPE: 8}):
                raise ValidationError("carry macro is not one CARRY8 plus eight LUT6_2")
            if bels != {"CARRY8", *{f"{letter}6LUT" for letter in "ABCDEFGH"}}:
                raise ValidationError("carry macro does not cover exact full-slice BEL roles")
            carry_macros.append({
                "cluster": str(cluster.get("id")),
                "members": sorted(macro_members, key=lambda item: item["instance"]),
            })
    physical = {
        name for name, cell in cells.items()
        if isinstance(cell, Mapping) and cell.get("type") not in CONSTANT_TYPES
    }
    if owners != physical:
        raise ValidationError("CARRY8 native qualification has incomplete cell coverage")
    if not carry_macros:
        raise ValidationError("CARRY8 native qualification requires a carry macro")
    if not any(atom["resource"] == "FF" for atom in atoms):
        raise ValidationError("CARRY8 native qualification requires at least one FF atom")
    cascades = packed.get("cascade_chains")
    if not isinstance(cascades, list):
        raise ValidationError("PackedSiteNetlist cascade chains are invalid")
    for chain in cascades:
        if not isinstance(chain, Mapping) or chain.get("cell_type") != "CARRY8":
            raise ValidationError("CARRY8 qualification rejects non-carry cascades")
    return (
        selected_top,
        cells,
        sorted(atoms, key=lambda item: item["instance"]),
        carry_macros,
    )


def _render_library(
    cells: Mapping[str, Any], atoms: Sequence[Mapping[str, str]]
) -> str:
    ports_by_type: Dict[str, Dict[str, str]] = defaultdict(dict)
    for atom in atoms:
        for port, direction, _bit in _expanded_pins(cells[atom["instance"]]):
            previous = ports_by_type[atom["cell_type"]].setdefault(port, direction)
            if previous != direction:
                raise ValidationError("primitive pin direction is inconsistent")
    blocks = []
    for cell_type, ports in sorted(ports_by_type.items()):
        lines = [f"CELL {cell_type}"]
        for port, direction in sorted(ports.items()):
            suffix = ""
            if cell_type in FF_TYPES and port == "C":
                suffix = " CLOCK"
            elif cell_type in FF_TYPES and port == "CE":
                suffix = " CTRL_CE"
            elif cell_type in FF_TYPES and port in {"R", "S"}:
                suffix = " CTRL_SR"
            elif cell_type == "CARRY8" and port == "CI":
                suffix = " CAS"
            elif cell_type == "CARRY8" and port == "CO[7]":
                suffix = " CAS"
            lines.append(f"  PIN {port} {direction.upper()}{suffix}")
        lines.append("END CELL")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _native_carry_edges(value: Mapping[str, Any]) -> set[Tuple[str, str]]:
    edges = set()
    for family in value.get("payload", {}).get("dedicated_adjacency", []):
        if family.get("kind") != "CARRY_NEXT":
            continue
        for chain in family.get("chains", []):
            edges.update(zip(chain, chain[1:]))
    if not edges:
        raise ValidationError("native constraints contain no CARRY_NEXT edges")
    return edges


def export_xilinx_openparf_carry8(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    native_constraints_path: Path,
    provider_manifest_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Export unplaced CARRY8 macros to the native OpenPARF chain route."""

    validate_xilinx_packing(
        mapped_path, packed_path, top=top, architecture_path=architecture_path
    )
    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("CARRY8 OpenPARF input is not a PackedSiteNetlist")
    architecture = ArchitectureDB.load(architecture_path)
    native, native_report = load_xilinx_native_device_constraints(
        native_constraints_path,
        architecture_path=architecture_path,
        provider_manifest_path=provider_manifest_path,
    )
    require_xilinx_native_constraint_capability(
        native_report, "dedicated_adjacency.CARRY_NEXT"
    )
    selected_top, cells, atoms, macros = _collect(mapped, packed, top)
    sites = _slice_sites(architecture)
    placement_region = _validate_native_placement_region(sites)
    per_site_capacity, density_contract = _validate_native_density_contract(
        sites, atoms
    )
    demand = Counter(atom["resource"] for atom in atoms)
    physical_capacity = {
        "LUT": 8 * len(sites), "FF": 16 * len(sites), "CARRY8": len(sites)
    }
    for resource, count in demand.items():
        if count > physical_capacity[resource]:
            raise ValidationError(
                f"CARRY8 route demand {count} exceeds {resource} capacity "
                f"{physical_capacity[resource]}"
            )
    names = {atom["instance"]: f"a{index}" for index, atom in enumerate(atoms)}
    library = _render_library(cells, atoms)
    nets, net_count, dropped = _render_nets(cells, atoms, names)
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
    model_map: Dict[str, Dict[str, Any]] = {}
    for primitive in sorted({atom["cell_type"] for atom in atoms}):
        if primitive in FF_TYPES:
            model_map[primitive] = {"FF": [0.25, 0.25], "isFF": 1}
        elif primitive in _SLICE_LUT_TYPES:
            model_map[primitive] = {
                "LUT": [0.25, 0.25],
                "isLUT": 6 if primitive == DUAL_OUTPUT_LUT_TYPE else int(primitive[3:]),
            }
        elif primitive == "CARRY8":
            model_map[primitive] = {"CARRY8": [1.0, 1.0]}
        else:
            raise ValidationError(f"unsupported CARRY8-route primitive {primitive!r}")
    config = {
        "benchmark_name": "xilinx_carry8_native",
        "benchmark_format": "bookshelf",
        "architecture_name": "ultrascale",
        "architecture_type": "ultrascale",
        "aux_input": str((output_dir / "design.aux").resolve()),
        "gpu": 0,
        "dtype": "float64",
        "target_density": _TARGET_DENSITY,
        "random_seed": 1000,
        "max_global_place_iters": 2000,
        "global_place_flag": 1,
        "legalize_flag": 1,
        "detailed_place_flag": 1,
        "generic_cluster_placement_flag": 0,
        "logic_area_type_names": ["LUT", "FF"],
        "plot_flag": 0,
        "plot_target_at_names": ["LUT", "FF", "CARRY8"],
        "io_at_names": [],
        "num_threads": 8,
        "gp_model2area_types_map": model_map,
        "gp_resource2area_types_map": {
            "LUT": ["LUT"], "FF": ["FF"], "CARRY8": ["CARRY8"]
        },
        "resource_categories": {
            "LUT": "LUTL", "FF": "FF", "CARRY8": "Carry"
        },
        "carry_chain_module_name": "CARRY8",
        "carry_chain_at_name": "CARRY8",
        "carry_chain_legalization_flag": 1,
        "align_carry_chain_flag": 1,
        "CLB_capacity": 16,
        "BLE_capacity": 2,
        "num_ControlSets_per_CLB": 2,
        "gp_adjust_area": 0,
        "gp_adjust_area_types": [],
        "gp_adjust_route_area": 0,
        "gp_adjust_pin_area": 0,
        "gp_adjust_resource_area": 0,
        "honor_clock_region_constraints": 0,
        "honor_half_column_constraints": 0,
        "route_flag": 0,
        "slr_aware_flag": 0,
        "result_dir": str((output_dir / "results").resolve()),
    }
    write_json(output_dir / "openparf.json", config, compact=True, sort_keys=False)
    write_json(output_dir / "name_map.json", {
        "schema": OPENPARF_CARRY8_NAME_MAP_SCHEMA,
        "top": selected_top,
        "atoms": [{"openparf": names[item["instance"]], **item} for item in atoms],
        "carry_macros": macros,
        "cascade_chains": packed.get("cascade_chains", []),
        "coordinate_system": coordinate_system,
        "source": {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "native_constraints_sha256": _sha256(native_constraints_path),
            "provider_manifest_sha256": _sha256(provider_manifest_path),
        },
    }, compact=True)
    manifest = {
        "schema": OPENPARF_CARRY8_MANIFEST_SCHEMA,
        "status": "pass",
        "provider": OPENPARF_CARRY8_PROVIDER,
        "atoms": len(atoms),
        "nets": net_count,
        "resources": dict(sorted(demand.items())),
        "macro_units": len(macros),
        "native_carry_edges": len(_native_carry_edges(native)),
        "placement_region": placement_region,
        "density_contract": density_contract,
        "resource_unit_capacity": per_site_capacity,
        "net_export": {"emitted": net_count, "dropped_single_endpoint": dropped},
        "preplacement": False,
        "fallback": "forbidden",
        "runtime_validation": "unverified",
    }
    write_json(output_dir / "manifest.json", manifest, compact=True)
    return manifest


def validate_xilinx_openparf_carry8_placement(
    placement_path: Path,
    name_map_path: Path,
    mapped_path: Path,
    architecture_path: Path,
    native_constraints_path: Path,
    provider_manifest_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently validate exact macro roles and native chain adjacency."""

    name_map = read_json(name_map_path)
    mapped = read_json(mapped_path)
    architecture = ArchitectureDB.load(architecture_path)
    native, native_report = load_xilinx_native_device_constraints(
        native_constraints_path,
        architecture_path=architecture_path,
        provider_manifest_path=provider_manifest_path,
    )
    require_xilinx_native_constraint_capability(
        native_report, "dedicated_adjacency.CARRY_NEXT"
    )
    if name_map.get("schema") != OPENPARF_CARRY8_NAME_MAP_SCHEMA:
        raise ValidationError("OpenPARF CARRY8 name map is invalid")
    source = name_map.get("source")
    expected_source = {
        "mapped_sha256": _sha256(mapped_path),
        "architecture_sha256": _sha256(architecture_path),
        "native_constraints_sha256": _sha256(native_constraints_path),
        "provider_manifest_sha256": _sha256(provider_manifest_path),
    }
    if not isinstance(source, Mapping) or any(
        source.get(key) != value for key, value in expected_source.items()
    ):
        raise ValidationError("OpenPARF CARRY8 source identity is invalid")
    _top, module = _select_module(mapped, name_map.get("top"))
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    atoms = {
        item["openparf"]: item for item in name_map.get("atoms", [])
        if isinstance(item, Mapping) and isinstance(item.get("openparf"), str)
    }
    if not atoms or len(atoms) != len(name_map.get("atoms", [])):
        raise ValidationError("OpenPARF CARRY8 name map has duplicate atoms")
    site_map = {
        (entry["dense_x"], entry["dense_y"]): entry
        for entry in name_map.get("coordinate_system", {}).get("sites", [])
        if isinstance(entry, Mapping)
    }
    placed: Dict[str, Dict[str, Any]] = {}
    occupied = set()
    with placement_path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.strip().split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) not in {4, 5} or fields[0] not in atoms:
                raise ImportError(f"{placement_path}:{line_number}: invalid placement row")
            if fields[0] in placed:
                raise ValidationError("OpenPARF CARRY8 placement duplicates an atom")
            try:
                xyz = [float(value) for value in fields[1:4]]
            except ValueError as error:
                raise ImportError("OpenPARF CARRY8 placement is non-numeric") from error
            if any(not math.isfinite(value) or not value.is_integer() for value in xyz):
                raise ValidationError("OpenPARF CARRY8 placement is not discrete")
            x, y, z = (int(value) for value in xyz)
            site_entry = site_map.get((x, y))
            if site_entry is None:
                raise ValidationError("OpenPARF CARRY8 placement uses an unknown site")
            atom = atoms[fields[0]]
            cell_type = atom["cell_type"]
            resource = atom["resource"]
            if resource == "LUT":
                if z not in range(1, 16, 2):
                    raise ValidationError("CARRY8 LUT6_2 must occupy a 6LUT z slot")
                bel_name = f"{'ABCDEFGH'[z // 2]}6LUT"
            elif resource == "FF":
                if not 0 <= z < 16:
                    raise ValidationError("CARRY8-route FF uses an invalid z slot")
                bel_name = f"{'ABCDEFGH'[z // 2]}FF" + ("2" if z % 2 else "")
            elif resource == "CARRY8":
                if z != 0:
                    raise ValidationError("CARRY8 must occupy its unique z=0 resource")
                bel_name = "CARRY8"
            else:
                raise ValidationError("OpenPARF CARRY8 placement has unknown resource")
            collision = (site_entry["site"], resource, z)
            if collision in occupied:
                raise ValidationError("OpenPARF CARRY8 placement overlaps a BEL slot")
            occupied.add(collision)
            site = architecture.site_named(site_entry["site"])
            compatible = [
                bel for bel in site.get("bels", [])
                if bel.get("name") == bel_name
                and cell_type in bel.get("compatible_cells", [])
            ]
            if len(compatible) != 1:
                raise ValidationError("OpenPARF CARRY8 placement has no compatible BEL")
            placed[fields[0]] = {
                **atom,
                "site": site_entry["site"],
                "z": z,
                "bel": bel_name,
                "placement_mode": compatible[0].get("placement_mode", site["type"]),
            }
    if set(placed) != set(atoms):
        raise ValidationError("OpenPARF CARRY8 placement coverage is incomplete")
    by_instance = {item["instance"]: item for item in placed.values()}
    carry_sites: Dict[str, str] = {}
    for macro in name_map.get("carry_macros", []):
        members = [by_instance[item["instance"]] for item in macro["members"]]
        sites = {item["site"] for item in members}
        bels = {item["bel"] for item in members}
        if len(sites) != 1 or bels != {
            "CARRY8", *{f"{letter}6LUT" for letter in "ABCDEFGH"}
        }:
            raise ValidationError("OpenPARF split or corrupted a CARRY8 full-slice macro")
        carry = next(item for item in members if item["cell_type"] == "CARRY8")
        carry_sites[carry["instance"]] = carry["site"]
    native_edges = _native_carry_edges(native)
    checked_edges = 0
    for chain in name_map.get("cascade_chains", []):
        instances = chain.get("instances", [])
        for source_name, target_name in zip(instances, instances[1:]):
            if (carry_sites[source_name], carry_sites[target_name]) not in native_edges:
                raise ValidationError("OpenPARF CARRY8 chain does not use native adjacency")
            checked_edges += 1
    by_site: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in placed.values():
        by_site[item["site"]].append(item)
    clusters = []
    for site_name, items in sorted(by_site.items()):
        site = architecture.site_named(site_name)
        clusters.append({
            "cluster": f"openparf:{site_name}",
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "assignments": [{
                "instance": item["instance"],
                "cell_type": item["cell_type"],
                "bel": item["bel"],
                "placement_mode": item["placement_mode"],
                "source_cluster": item["source_cluster"],
            } for item in sorted(items, key=lambda value: value["instance"])],
        })
    result = {
        "schema": OPENPARF_CARRY8_PLACEMENT_SCHEMA,
        "status": "pass",
        "provider": OPENPARF_CARRY8_PROVIDER,
        "part": architecture.part,
        "runtime_validation": "unverified",
        "source": {
            "native_placement_sha256": _sha256(placement_path),
            "name_map_sha256": _sha256(name_map_path),
            "mapped_sha256": _sha256(mapped_path),
            "architecture_sha256": _sha256(architecture_path),
            "native_constraints_sha256": _sha256(native_constraints_path),
        },
        "clusters": clusters,
        "summary": {
            "atoms": len(placed),
            "occupied_sites": len(clusters),
            "carry8_macros": len(carry_sites),
            "native_carry_edges": checked_edges,
        },
    }
    if output_path is not None:
        write_json(output_path, result, compact=True)
    return result


def run_xilinx_openparf_carry8_qualification(
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
    """Run native GP -> CARRY8 legalization -> masked LG -> ISM."""

    runtime = validate_openparf_runtime(
        install_root=openparf_install, python_executable=openparf_python
    )
    manifest = export_xilinx_openparf_carry8(
        mapped_path,
        packed_path,
        architecture_path,
        native_constraints_path,
        provider_manifest_path,
        output_dir,
        top=top,
    )
    placement = run_openparf(
        output_dir / "openparf.json",
        log_path=output_dir / "openparf.log",
        install_root=openparf_install,
        python_executable=openparf_python,
    )
    certificate = validate_xilinx_openparf_carry8_placement(
        placement,
        output_dir / "name_map.json",
        mapped_path,
        architecture_path,
        native_constraints_path,
        provider_manifest_path,
        output_dir / "placement-certificate.json",
    )
    installation = Path(str(runtime.get("installation", "")))
    python = Path(str(runtime.get("python", "")))
    if (installation / "openparf.py").is_file() and python.is_file():
        certificate["runtime_validation"] = "native-openparf"
        write_json(output_dir / "placement-certificate.json", certificate, compact=True)
    return {
        "status": "pass",
        "runtime": runtime,
        "manifest": manifest,
        "placement": str(placement),
        "certificate": certificate,
        "qualification_scope": "CARRY8 plus eight LUT6_2 full-slice macros and native CARRY_NEXT adjacency",
    }
