"""OpenPARF global guidance for exact Xilinx packed clusters.

OpenPARF optimizes one movable node per packed cluster. Its output is only a
global coordinate hint; :mod:`emuflow.xilinx_placement` remains authoritative
for exact site, BEL, fixed-region, and cascade legality.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .architecture import ArchitectureDB
from .errors import ImportError, ValidationError
from .io import read_json, write_json
from .openparf import run_openparf
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_GUIDANCE_SCHEMA


XILINX_OPENPARF_MANIFEST_SCHEMA = "emuflow.xilinx-openparf-manifest/v1"


def _cluster_resource(cluster: Mapping[str, Any]) -> str:
    kind = cluster.get("kind")
    if kind in {"slice", "carry"}:
        return "X_SLICE"
    types = {item.get("cell_type") for item in cluster.get("assignments", [])}
    if types == {"DSP48E2"}:
        return "X_DSP"
    if types and all(str(cell_type).startswith("RAMB") for cell_type in types):
        return "X_BRAM"
    if types == {"URAM288"}:
        return "X_URAM"
    raise ValidationError(
        f"cluster {cluster.get('id')!r} has no OpenPARF resource class"
    )


def _site_resource(site_type: str) -> Optional[str]:
    upper = site_type.upper()
    if upper.startswith("SLICE"):
        return "X_SLICE"
    if upper.startswith("DSP"):
        return "X_DSP"
    if upper.startswith("RAMB"):
        return "X_BRAM"
    if upper.startswith("URAM"):
        return "X_URAM"
    return None


def _select_module(mapped: Mapping[str, Any], top: Optional[str]) -> Mapping[str, Any]:
    modules = mapped.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped JSON modules are invalid")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped JSON has no top module {top!r}")
        return module
    selected = [
        module for module in modules.values()
        if isinstance(module, dict)
        and str(module.get("attributes", {}).get("top", "0")) == "1"
    ]
    if len(selected) == 1:
        return selected[0]
    if len(modules) == 1:
        return next(iter(modules.values()))
    raise ValidationError("mapped JSON top module is ambiguous")


def _cluster_nets(
    mapped: Mapping[str, Any], packed: Mapping[str, Any], top: Optional[str]
) -> List[Tuple[str, str, List[str]]]:
    cells = _select_module(mapped, top).get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped JSON cells are invalid")
    owner = {
        assignment["instance"]: cluster["id"]
        for cluster in packed["clusters"]
        for assignment in cluster["assignments"]
    }
    drivers: Dict[Any, Set[str]] = defaultdict(set)
    sinks: Dict[Any, Set[str]] = defaultdict(set)
    for instance, cell in cells.items():
        cluster = owner.get(instance)
        if cluster is None:
            continue
        directions = cell.get("port_directions", {})
        for port, bits in cell.get("connections", {}).items():
            direction = directions.get(port)
            target = drivers if direction in {"output", "inout"} else sinks
            if not isinstance(bits, list):
                raise ValidationError(f"cell {instance!r} port {port!r} is invalid")
            for bit in bits:
                if isinstance(bit, int):
                    target[bit].add(cluster)
    result = []
    for bit in sorted(set(drivers) | set(sinks)):
        bit_drivers = drivers.get(bit, set())
        if len(bit_drivers) > 1:
            raise ValidationError(f"mapped bit {bit} has multiple cluster drivers")
        if not bit_drivers:
            continue
        driver = next(iter(bit_drivers))
        bit_sinks = sorted(sinks.get(bit, set()) - {driver})
        if bit_sinks:
            result.append((f"n{bit}", driver, bit_sinks))
    return result


def _render_library(
    resources: Mapping[str, str], nets: List[Tuple[str, str, List[str]]]
) -> str:
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    for _net, driver, sinks in nets:
        outgoing[driver] += 1
        incoming.update(sinks)
    maxima: Dict[str, Tuple[int, int]] = {}
    for cluster, resource in resources.items():
        old = maxima.get(resource, (0, 0))
        maxima[resource] = (
            max(old[0], outgoing[cluster]), max(old[1], incoming[cluster])
        )
    blocks = []
    for resource, (outputs, inputs) in sorted(maxima.items()):
        lines = [f"CELL {resource}"]
        lines.extend(f"  PIN O{index} OUTPUT" for index in range(outputs))
        lines.extend(f"  PIN I{index} INPUT" for index in range(inputs))
        lines.append("  PIN EMUFLOW_CONST INPUT CTRL_SR")
        lines.append("END CELL")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_nets(
    names: Mapping[str, str], nets: List[Tuple[str, str, List[str]]]
) -> str:
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    lines = []
    for safe_index, (_net, driver, sinks) in enumerate(nets):
        lines.append(f"net n{safe_index} {1 + len(sinks)}")
        lines.append(f"  {names[driver]} O{outgoing[driver]}")
        outgoing[driver] += 1
        for sink in sinks:
            lines.append(f"  {names[sink]} I{incoming[sink]}")
            incoming[sink] += 1
        lines.append("endnet")
    return "\n".join(lines) + "\n"


def _render_sites(architecture: ArchitectureDB, used: Set[str]) -> str:
    resources_by_type: Dict[str, Optional[str]] = {}
    for site in architecture.value["sites"]:
        resource = _site_resource(site["type"])
        existing = resources_by_type.setdefault(site["type"], resource)
        if existing != resource:
            raise ValidationError(f"inconsistent resource class for {site['type']}")
    lines = []
    for site_type, resource in sorted(resources_by_type.items()):
        lines.append(f"SITE {site_type}")
        if resource in used:
            lines.append(f"  {resource} 1")
        lines.extend(["END SITE", ""])
    lines.append("RESOURCES")
    for resource in sorted(used):
        lines.append(f"  {resource} {resource}")
    lines.extend(["END RESOURCES", ""])
    width = max(site["x"] for site in architecture.value["sites"]) + 1
    height = max(site["y"] for site in architecture.value["sites"]) + 1
    lines.append(f"SITEMAP {width} {height}")
    lines.extend(
        f"{site['x']} {site['y']} {site['type']}"
        for site in sorted(
            architecture.value["sites"], key=lambda item: (item["x"], item["y"])
        )
    )
    lines.append("END SITEMAP")
    return "\n".join(lines) + "\n"


def export_xilinx_cluster_bookshelf(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    names = {
        cluster["id"]: f"c{index}"
        for index, cluster in enumerate(sorted(packed["clusters"], key=lambda x: x["id"]))
    }
    resources = {
        cluster["id"]: _cluster_resource(cluster) for cluster in packed["clusters"]
    }
    selected_top = top if top is not None else packed.get("top")
    nets = _cluster_nets(mapped, packed, selected_top)
    demand = Counter(resources.values())
    capacity = Counter(
        resource for resource in (
            _site_resource(site["type"]) for site in architecture.value["sites"]
        ) if resource is not None
    )
    for resource, count in demand.items():
        if count > capacity[resource]:
            raise ValidationError(
                f"ArchitectureDB has {capacity[resource]} {resource} sites for {count} clusters"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "design.nodes": "".join(
            f"{names[cluster]} {resources[cluster]}\n" for cluster in sorted(names)
        ),
        "design.lib": _render_library(resources, nets),
        "design.nets": _render_nets(names, nets),
        "design.scl": _render_sites(architecture, set(demand)),
        "design.pl": "",
        "design.aux": "design : design.nodes design.nets design.pl design.scl design.lib\n",
    }
    for name, content in files.items():
        (output_dir / name).write_text(content, encoding="utf-8")
    utilization = max(
        (count / capacity[resource] for resource, count in demand.items()), default=0.0
    )
    model_map = {
        resource: {resource: ["1", "1"], "isLUT": 0, "isFF": 0}
        for resource in sorted(demand)
    }
    config = {
        "benchmark_name": "xilinx_clusters", "benchmark_format": "bookshelf",
        "architecture_name": "ultrascale",
        "aux_input": str((output_dir / "design.aux").resolve()),
        "gpu": 0, "dtype": "float64",
        "target_density": max(0.8, min(0.995, utilization + 0.005)),
        "random_seed": 1000, "max_global_place_iters": 1000,
        "global_place_flag": 1, "legalize_flag": 1,
        "generic_cluster_placement_flag": 1,
        "logic_area_type_names": sorted(demand),
        "detailed_place_flag": 0, "plot_flag": 0,
        "plot_target_at_names": sorted(demand), "io_at_names": [],
        "num_threads": 8, "gp_model2area_types_map": model_map,
        "gp_resource2area_types_map": {
            resource: [resource] for resource in sorted(demand)
        },
        "resource_categories": {resource: "SSSIR" for resource in sorted(demand)},
        "CLB_capacity": 1, "BLE_capacity": 1, "num_ControlSets_per_CLB": 1,
        "gp_adjust_area": 0, "gp_adjust_area_types": [],
        "gp_adjust_route_area": 0, "gp_adjust_pin_area": 0,
        "gp_adjust_resource_area": 0, "honor_clock_region_constraints": 0,
        "honor_half_column_constraints": 0,
        "result_dir": str((output_dir / "results").resolve()),
        "route_flag": 0, "slr_aware_flag": 0,
    }
    write_json(output_dir / "openparf.json", config, compact=True)
    write_json(output_dir / "name_map.json", {
        "schema": "emuflow.xilinx-openparf-name-map/v1",
        "clusters": [
            {"openparf": names[cluster], "cluster": cluster}
            for cluster in sorted(names)
        ],
    }, compact=True)
    manifest = {
        "schema": XILINX_OPENPARF_MANIFEST_SCHEMA,
        "status": "pass", "part": architecture.part,
        "clusters": len(names), "nets": len(nets),
        "resources": dict(sorted(demand.items())),
        "files": sorted([*files, "openparf.json", "name_map.json"]),
    }
    write_json(output_dir / "manifest.json", manifest, compact=True)
    return manifest


def import_xilinx_openparf_guidance(
    placement_path: Path, name_map_path: Path, output_path: Path
) -> Dict[str, Any]:
    name_map = read_json(name_map_path)
    if name_map.get("schema") != "emuflow.xilinx-openparf-name-map/v1":
        raise ValidationError("Xilinx OpenPARF name map is invalid")
    safe_to_cluster = {
        entry["openparf"]: entry["cluster"] for entry in name_map["clusters"]
    }
    coordinates = {}
    with placement_path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) not in {3, 4, 5} or fields[0] not in safe_to_cluster:
                raise ImportError(f"{placement_path}:{line_number}: invalid OpenPARF placement")
            cluster = safe_to_cluster[fields[0]]
            if cluster in coordinates:
                raise ImportError(f"{placement_path}:{line_number}: duplicate cluster")
            try:
                coordinates[cluster] = (float(fields[1]), float(fields[2]))
            except ValueError as error:
                raise ImportError(
                    f"{placement_path}:{line_number}: non-numeric coordinate"
                ) from error
    if set(coordinates) != set(safe_to_cluster.values()):
        raise ImportError("OpenPARF placement does not cover every packed cluster")
    result = {
        "schema": XILINX_GUIDANCE_SCHEMA,
        "provider": "openparf-global-guidance-v1",
        "clusters": [
            {"cluster": cluster, "x": coordinates[cluster][0], "y": coordinates[cluster][1]}
            for cluster in sorted(coordinates)
        ],
    }
    write_json(output_path, result, compact=True)
    return {"status": "pass", "clusters": len(coordinates), "output": str(output_path)}


def run_xilinx_openparf_guidance(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    output_dir: Path,
    *,
    top: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
) -> Dict[str, Any]:
    manifest = export_xilinx_cluster_bookshelf(
        mapped_path, packed_path, architecture_path, output_dir, top=top
    )
    placement = run_openparf(
        output_dir / "openparf.json",
        log_path=output_dir / "openparf.log",
        install_root=openparf_install,
        python_executable=openparf_python,
    )
    report = import_xilinx_openparf_guidance(
        placement, output_dir / "name_map.json", output_dir / "guidance.json"
    )
    return {**report, "manifest": manifest["schema"], "placement": str(placement)}
