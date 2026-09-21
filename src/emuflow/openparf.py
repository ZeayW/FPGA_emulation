from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from .architecture import ArchitectureDB
from .errors import EmuFlowError
from .io import write_json
from .ir import EmuIR
from .native_tools import native_install_roots


OPENPARF_MANIFEST_SCHEMA = "emuflow.openparf-manifest/v1"
OPENPARF_NAME_MAP_SCHEMA = "emuflow.openparf-name-map/v1"

_KNOWN_CELL_PINS: Dict[str, Dict[str, str]] = {
    "FDRE": {
        "C": "INPUT",
        "CE": "INPUT",
        "D": "INPUT",
        "Q": "OUTPUT",
        "R": "INPUT",
    },
}


def resolve_openparf_install(
    explicit: Optional[Path] = None,
) -> Path:
    """Resolve only an explicit or root-build OpenPARF installation."""
    if explicit is not None:
        root = explicit.expanduser()
        # Accept both the OpenPARF package directory itself and the monorepo
        # CMake install prefix that contains it.  The CLI option is named
        # --openparf-install, so both layouts are natural and unambiguous.
        candidates = (root, root / "openparf")
    else:
        candidates = tuple(root / "openparf" for root in native_install_roots())
    for candidate in candidates:
        resolved = candidate.resolve()
        if (
            (resolved / "openparf.py").is_file()
            and (resolved / "openparf").is_dir()
        ):
            return resolved
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise EmuFlowError(
        "in-tree OpenPARF build product was not found; searched: "
        f"{searched}. Build the monorepo with `cmake --preset release && "
        "cmake --build --preset release`."
    )


def _openparf_environment(installation: Path) -> Dict[str, str]:
    environment = os.environ.copy()
    python_path = str(installation)
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    return environment


def validate_openparf_runtime(
    *,
    install_root: Optional[Path] = None,
    python_executable: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fail before physical work when the configured OpenPARF runtime is unusable."""

    installation = resolve_openparf_install(install_root)
    python = (
        python_executable
        if python_executable is not None
        else Path(os.environ.get("EMUFLOW_OPENPARF_PYTHON", sys.executable))
    ).expanduser()
    if not python.is_file():
        raise EmuFlowError(
            f"OpenPARF Python interpreter does not exist: {python}"
        )
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                "import torch; from openparf.flow import place, route",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=120,
            env=_openparf_environment(installation),
        )
    except subprocess.TimeoutExpired as error:
        raise EmuFlowError(
            "OpenPARF runtime preflight exceeded 120 seconds"
        ) from error
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-40:])
        raise EmuFlowError(
            "OpenPARF runtime preflight failed before physical execution"
            + (f":\n{tail}" if tail else "")
        )
    return {
        "status": "pass",
        "installation": str(installation),
        "python": str(python),
    }


def run_openparf(
    config_path: Path,
    *,
    log_path: Optional[Path] = None,
    install_root: Optional[Path] = None,
    python_executable: Optional[Path] = None,
) -> Path:
    """Run OpenPARF built from this repository and return its .pl output."""
    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmuFlowError(
            f"cannot load OpenPARF configuration {config_path}: {error}"
        ) from error
    benchmark = config.get("benchmark_name")
    result_dir = config.get("result_dir")
    if (
        not isinstance(benchmark, str)
        or not benchmark
        or not isinstance(result_dir, str)
        or not result_dir
    ):
        raise EmuFlowError(
            "OpenPARF configuration requires benchmark_name and result_dir"
        )
    installation = resolve_openparf_install(install_root)
    python = (
        python_executable
        if python_executable is not None
        else Path(
            os.environ.get("EMUFLOW_OPENPARF_PYTHON", sys.executable)
        )
    ).expanduser()
    if not python.is_file():
        raise EmuFlowError(
            f"OpenPARF Python interpreter does not exist: {python}"
        )
    if log_path is None:
        log_path = config_path.with_suffix(".log")
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    console_log_path = log_path.with_name(
        f"{log_path.stem}.console{log_path.suffix}"
    )
    environment = _openparf_environment(installation)
    continuous_guidance = config.get("emuflow_continuous_global_guidance", False)
    if not isinstance(continuous_guidance, bool):
        raise EmuFlowError(
            "emuflow_continuous_global_guidance must be a boolean"
        )
    command = (
        [
            str(python.absolute()),
            "-m",
            "emuflow.openparf_continuous_driver",
        ]
        if continuous_guidance
        else [
            str(python.absolute()),
            str(installation / "openparf.py"),
        ]
    )
    with console_log_path.open("w", encoding="utf-8") as console_log:
        completed = subprocess.run(
            [
                # Preserve a virtual-environment launcher path. Resolving its
                # symlink to /usr/bin/python bypasses pyvenv.cfg and silently
                # loses the PyTorch environment used to build OpenPARF.
                *command,
                "--config",
                str(config_path),
                "--log",
                str(log_path),
            ],
            cwd=config_path.parent,
            env=environment,
            stdout=console_log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        try:
            lines = console_log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            detail = "\n".join(lines[-40:])
        except OSError:
            detail = f"see {console_log_path}"
        raise EmuFlowError(
            "in-tree OpenPARF failed with exit code "
            f"{completed.returncode}: {detail}"
        )
    placement = Path(result_dir) / f"{benchmark}.pl"
    if not placement.is_file():
        raise EmuFlowError(
            "in-tree OpenPARF completed without expected placement: "
            f"{placement}"
        )
    return placement.resolve()


def _pins_by_cell_type(ir: EmuIR) -> Dict[str, Dict[str, str]]:
    instance_types = {
        instance["id"]: instance["type"] for instance in ir.value["instances"]
    }
    result: Dict[str, Dict[str, str]] = defaultdict(dict)
    for cell_type in instance_types.values():
        result[cell_type].update(_KNOWN_CELL_PINS.get(cell_type, {}))
    for net in ir.value["nets"]:
        for endpoint in net["drivers"]:
            instance = endpoint.get("instance")
            if instance is not None:
                result[instance_types[instance]][endpoint["port"]] = "OUTPUT"
        for endpoint in net["sinks"]:
            instance = endpoint.get("instance")
            if instance is not None:
                result[instance_types[instance]][endpoint["port"]] = "INPUT"
    return result


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def openparf_instance_names(ir: EmuIR) -> Dict[str, str]:
    """Map arbitrary Yosys instance names to Bookshelf-safe stable names."""
    return {
        instance["id"]: f"i{index}"
        for index, instance in enumerate(
            sorted(ir.value["instances"], key=lambda item: item["id"])
        )
    }


def _render_nodes(ir: EmuIR, instance_names: Mapping[str, str]) -> str:
    return "".join(
        f"{instance_names[instance['id']]} {instance['type']}\n"
        for instance in sorted(ir.value["instances"], key=lambda item: item["id"])
    )


def _render_lib(ir: EmuIR) -> str:
    blocks: List[str] = []
    for cell_type, pins in sorted(_pins_by_cell_type(ir).items()):
        lines = [f"CELL {cell_type}"]
        for pin, direction in sorted(pins.items()):
            qualifier = ""
            if direction == "INPUT" and pin in {"C", "CLK"}:
                qualifier = " CLOCK"
            elif direction == "INPUT" and pin == "CE":
                qualifier = " CTRL_CE"
            elif direction == "INPUT" and pin in {
                "R",
                "S",
                "CLR",
                "PRE",
            }:
                qualifier = " CTRL_SR"
            lines.append(f"  PIN {pin} {direction}{qualifier}")
        lines.append("END CELL")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _net_endpoints(
    net: Mapping[str, Any], instance_names: Mapping[str, str]
) -> Iterable[Tuple[str, str]]:
    for collection in ("drivers", "sinks"):
        for endpoint in net[collection]:
            instance = endpoint.get("instance")
            if instance is not None:
                yield instance_names[instance], endpoint["port"]


def _render_nets(
    ir: EmuIR, instance_names: Mapping[str, str]
) -> Tuple[str, int, Dict[str, str]]:
    lines: List[str] = []
    emitted = 0
    net_names: Dict[str, str] = {}
    for net in sorted(ir.value["nets"], key=lambda item: item["id"]):
        endpoints = list(_net_endpoints(net, instance_names))
        if len(endpoints) < 2:
            continue
        safe_name = f"n{emitted}"
        net_names[net["id"]] = safe_name
        emitted += 1
        lines.append(f"net {safe_name} {len(endpoints)}")
        lines.extend(f"  {instance} {pin}" for instance, pin in endpoints)
        lines.append("endnet")
    return "\n".join(lines) + "\n", emitted, net_names


def _site_resource_counts(site: Mapping[str, Any]) -> Dict[str, int]:
    slots: Dict[str, Set[str]] = defaultdict(set)
    for bel in site["bels"]:
        for cell_type in bel["compatible_cells"]:
            if cell_type.startswith("LUT"):
                slots["LUT"].add(bel["name"])
            elif cell_type.startswith("FD"):
                slots["FF"].add(bel["name"])
            elif cell_type == "CARRY8":
                slots["CARRY8"].add(bel["name"])
            elif cell_type == "DSP48E2":
                slots["DSP"].add(bel["name"])
            elif cell_type == "RAMB36E2":
                slots["RAM"].add(bel["name"])
    return {resource: len(bels) for resource, bels in slots.items()}


def _render_scl(ir: EmuIR, architecture: ArchitectureDB) -> str:
    by_site_type: Dict[str, Dict[str, int]] = {}
    for site in architecture.sites:
        resources = _site_resource_counts(site)
        existing = by_site_type.setdefault(site["type"], resources)
        if existing != resources:
            raise ValueError(
                f"site type {site['type']!r} has inconsistent resource counts"
            )
    lines: List[str] = []
    for site_type, resources in sorted(by_site_type.items()):
        lines.append(f"SITE {site_type}")
        for resource, count in sorted(resources.items()):
            lines.append(f"  {resource} {count}")
        lines.extend(["END SITE", ""])
    cell_types = sorted(
        {instance["type"] for instance in ir.value["instances"]}
    )
    resource_cells: Dict[str, List[str]] = defaultdict(list)
    for cell_type in cell_types:
        if cell_type.startswith("LUT"):
            resource_cells["LUT"].append(cell_type)
        elif cell_type.startswith("FD"):
            resource_cells["FF"].append(cell_type)
        elif cell_type == "CARRY8":
            resource_cells["CARRY8"].append(cell_type)
        elif cell_type == "DSP48E2":
            resource_cells["DSP"].append(cell_type)
        elif cell_type == "RAMB36E2":
            resource_cells["RAM"].append(cell_type)
    lines.append("RESOURCES")
    for resource, types in sorted(resource_cells.items()):
        lines.append(f"  {resource} {' '.join(types)}")
    lines.extend(["END RESOURCES", ""])
    width = max(site["x"] for site in architecture.sites) + 1
    height = max(site["y"] for site in architecture.sites) + 1
    lines.append(f"SITEMAP {width} {height}")
    for site in sorted(
        architecture.sites, key=lambda item: (item["x"], item["y"])
    ):
        lines.append(f"{site['x']} {site['y']} {site['type']}")
    lines.append("END SITEMAP")
    return "\n".join(lines) + "\n"


def _area_type_for_cell(cell_type: str) -> str:
    if cell_type.startswith("LUT"):
        return "LUT"
    if cell_type.startswith("FD"):
        return "FF"
    if cell_type == "CARRY8":
        return "CARRY8"
    if cell_type == "DSP48E2":
        return "DSP"
    if cell_type == "RAMB36E2":
        return "RAM"
    raise ValueError(f"OpenPARF adapter does not support cell type {cell_type!r}")


def _lut_size(cell_type: str) -> int:
    if not cell_type.startswith("LUT"):
        return 0
    try:
        size = int(cell_type[3:])
    except ValueError as error:
        raise ValueError(f"invalid LUT model name {cell_type!r}") from error
    if size < 1 or size > 6:
        raise ValueError(f"OpenPARF supports LUT1 through LUT6, got {cell_type!r}")
    # OpenPARF's LUT demand operator asserts that LUT1 does not exist. Model a
    # physical LUT1 with LUT2 demand during placement; the EmuIR cell type and
    # final Site/BEL assignment remain LUT1, so functionality is unchanged.
    return max(2, size)


def _render_config(
    ir: EmuIR, architecture: ArchitectureDB, output_dir: Path
) -> Dict[str, Any]:
    types = sorted({instance["type"] for instance in ir.value["instances"]})
    demand_by_area_type = Counter(
        _area_type_for_cell(instance["type"])
        for instance in ir.value["instances"]
    )
    capacity_by_area_type = Counter()
    for site in architecture.sites:
        capacity_by_area_type.update(_site_resource_counts(site))
    utilizations = [
        demand / capacity_by_area_type[area_type]
        for area_type, demand in demand_by_area_type.items()
        if capacity_by_area_type[area_type] > 0
    ]
    max_resource_utilization = max(utilizations, default=0.0)
    target_density = max(
        0.8,
        min(0.98, max_resource_utilization + 0.05),
    )
    model_map: Dict[str, Any] = {}
    resource_map: Dict[str, str] = {}
    resource_categories: Dict[str, str] = {}
    for cell_type in types:
        area_type = _area_type_for_cell(cell_type)
        site_capacity = max(
            _site_resource_counts(site).get(area_type, 0)
            for site in architecture.sites
        )
        if site_capacity <= 0:
            raise ValueError(
                f"ArchitectureDB has no {area_type} slots for {cell_type}"
            )
        entry: Dict[str, Any] = {
            area_type: [
                f"sqrt(1/{site_capacity})",
                f"sqrt(1/{site_capacity})",
            ],
            "isLUT": _lut_size(cell_type),
            "isFF": int(cell_type.startswith("FD")),
        }
        model_map[cell_type] = entry
        resource_map[area_type] = area_type
        if area_type == "LUT":
            resource_categories[area_type] = "LUTL"
        elif area_type == "FF":
            resource_categories[area_type] = "FF"
        else:
            resource_categories[area_type] = "SSSIR"
    return {
        "benchmark_name": ir.value["design"]["name"],
        "benchmark_format": "bookshelf",
        "architecture_name": "ultrascale",
        "aux_input": str(output_dir / "design.aux"),
        "gpu": 0,
        "dtype": "float64",
        # Density must exceed the most utilized heterogeneous resource.
        # A fixed 0.8 target is mathematically infeasible for dense designs
        # such as NVDLA partition A (about 85% LUT utilization).
        "target_density": target_density,
        "random_seed": 1000,
        # Production-scale designs need enough density iterations to spread
        # before legalization. A 100-iteration smoke-test limit left the
        # 121k-cell regression at ~0.96 overflow and produced unroutable
        # level-6 congestion after legalization.
        "max_global_place_iters": 1000,
        "global_place_flag": 1,
        "legalize_flag": 1,
        # OpenPARF's ISM detailed placer assumes a production-scale design and
        # fails on the intentionally tiny Phase 2 smoke test. Global placement
        # plus UltraScale slot legalization already produces a legal .pl file.
        "detailed_place_flag": 0,
        "plot_flag": 0,
        "plot_target_at_names": sorted(resource_map),
        "io_at_names": [],
        "num_threads": 8,
        "gp_model2area_types_map": model_map,
        "gp_resource2area_types_map": resource_map,
        "resource_categories": resource_categories,
        "CLB_capacity": max(
            (_site_resource_counts(site).get("LUT", 0)
             for site in architecture.sites),
            default=8,
        ),
        "BLE_capacity": 2,
        "num_ControlSets_per_CLB": 4,
        "gp_adjust_area": 0,
        "gp_adjust_area_types": [],
        "gp_adjust_route_area": 0,
        "gp_adjust_pin_area": 0,
        "gp_adjust_resource_area": 0,
        "honor_clock_region_constraints": 0,
        "honor_half_column_constraints": 0,
        "result_dir": str(output_dir / "results"),
        "route_flag": 0,
        "slr_aware_flag": 0,
    }


def export_bookshelf(
    ir: EmuIR, architecture: ArchitectureDB, output_dir: Path
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    instance_names = openparf_instance_names(ir)
    nets, emitted_nets, net_names = _render_nets(ir, instance_names)
    _write_text(output_dir / "design.nodes", _render_nodes(ir, instance_names))
    _write_text(output_dir / "design.nets", nets)
    _write_text(output_dir / "design.lib", _render_lib(ir))
    _write_text(output_dir / "design.scl", _render_scl(ir, architecture))
    _write_text(output_dir / "design.pl", "")
    _write_text(
        output_dir / "design.aux",
        "design : design.nodes design.nets design.pl design.scl design.lib\n",
    )
    write_json(
        output_dir / "openparf.json",
        _render_config(ir, architecture, output_dir.resolve()),
    )
    write_json(
        output_dir / "name_map.json",
        {
            "schema": OPENPARF_NAME_MAP_SCHEMA,
            "instances": [
                {"openparf": safe, "emuir": original}
                for original, safe in sorted(
                    instance_names.items(), key=lambda item: item[1]
                )
            ],
            "nets": [
                {"openparf": safe, "emuir": original}
                for original, safe in sorted(
                    net_names.items(), key=lambda item: item[1]
                )
            ],
        },
    )
    manifest = {
        "schema": OPENPARF_MANIFEST_SCHEMA,
        "design": ir.value["design"]["name"],
        "part": architecture.part,
        "instances": len(ir.value["instances"]),
        "nets": emitted_nets,
        "dropped_single_endpoint_nets": len(ir.value["nets"]) - emitted_nets,
        "coordinate_contract": (
            "OpenPARF x/y selects exactly one ArchitectureDB site; z plus "
            "cell type selects exactly one compatible BEL."
        ),
        "files": {
            "aux": "design.aux",
            "config": "openparf.json",
            "library": "design.lib",
            "name_map": "name_map.json",
            "nets": "design.nets",
            "nodes": "design.nodes",
            "placement": "design.pl",
            "sites": "design.scl",
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest
