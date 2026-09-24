"""Minimal, Vivado-free adapter boundary for the public AMF placer.

The adapter is deliberately limited to a single-region fixture scope. It
serializes AMF's public text formats and imports ``place_cell`` records as data;
it never executes Tcl. Production XCVU19P clock legality, multi-SLR placement,
MUXF9, and URAM288 remain fail-closed in :mod:`emuflow.amf_placer`.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_CONSTRAINTS_SCHEMA


AMF_DESIGN_ADAPTER_SCHEMA = "emuflow.amf-design-adapter/v1"
AMF_DEVICE_ADAPTER_SCHEMA = "emuflow.amf-device-adapter/v1"
AMF_RESULT_ADAPTER_SCHEMA = "emuflow.amf-result-adapter/v1"
AMF_FIXTURE_ROUNDTRIP_SCHEMA = "emuflow.amf-fixture-roundtrip/v1"

_CONSTANT_TYPES = {"GND": "0", "VCC": "1"}
_FIXTURE_CELL_TYPES = {
    *{f"LUT{width}" for width in range(1, 7)},
    "LUT6_2",
    "FDCE",
    "FDPE",
    "FDRE",
    "FDSE",
    "CARRY8",
}
_NAME_RE = re.compile(r"^\S+$")
_CLOCK_REGION_RE = re.compile(r"^X(?P<x>\d+)Y(?P<y>\d+)$")
_PLACE_CELL_OPEN_RE = re.compile(r"\bplace_cell\s*\{")


def _name(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or _NAME_RE.fullmatch(value) is None:
        raise ValidationError(f"{context} must be a non-empty token")
    return value


def _select_module(
    mapped: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any]]:
    modules = mapped.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValidationError("AMF design adapter mapped modules are invalid")
    if top is None:
        marked = [
            name
            for name, module in modules.items()
            if isinstance(module, Mapping)
            and isinstance(module.get("attributes"), Mapping)
            and str(module["attributes"].get("top", "0")) == "1"
        ]
        if len(marked) != 1:
            raise ValidationError(
                "AMF design adapter requires one marked top or an explicit top"
            )
        top = marked[0]
    module = modules.get(top)
    if not isinstance(module, Mapping):
        raise ValidationError(f"AMF design adapter top {top!r} is missing")
    return top, module


def _cell_ports(cell: Mapping[str, Any], context: str) -> Iterable[Tuple[str, str, List[Any]]]:
    directions = cell.get("port_directions")
    connections = cell.get("connections")
    if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
        raise ValidationError(f"{context} lacks explicit port metadata")
    if set(directions) != set(connections):
        raise ValidationError(f"{context} port directions and connections differ")
    for port in sorted(connections):
        direction = directions[port]
        bits = connections[port]
        if direction not in {"input", "output"} or not isinstance(bits, list):
            raise ValidationError(f"{context} port {port!r} is unsupported")
        yield _name(port, f"{context} port"), direction, bits


def export_amf_design(
    mapped: Mapping[str, Any], *, top: Optional[str] = None
) -> Dict[str, Any]:
    """Serialize a normalized Yosys module to AMF's public design text.

    GND/VCC pseudo-cells are removed only after their driven bits are converted
    to AMF's explicit ``<const0>``/``<const1>`` nets. Unknown, MUXF9, URAM288,
    floating integer inputs, and multiply-driven bits are rejected.
    """

    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("AMF design adapter cells are invalid")

    constant_bits: Dict[int, str] = {}
    constants: List[str] = []
    for raw_name, raw_cell in sorted(cells.items()):
        name = _name(raw_name, "AMF design cell")
        if not isinstance(raw_cell, Mapping):
            raise ValidationError(f"AMF design cell {name!r} is invalid")
        cell_type = raw_cell.get("type")
        if cell_type not in _CONSTANT_TYPES:
            continue
        constants.append(name)
        value = _CONSTANT_TYPES[cell_type]
        for _port, direction, bits in _cell_ports(raw_cell, f"cell {name!r}"):
            if direction != "output":
                raise ValidationError(f"constant cell {name!r} has an input")
            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    if bit in constant_bits and constant_bits[bit] != value:
                        raise ValidationError(f"constant bit {bit} has conflicting drivers")
                    constant_bits[bit] = value
                elif bit not in {"0", "1"}:
                    raise ValidationError(f"constant cell {name!r} has an invalid bit")

    drivers: Dict[int, str] = {}
    normalized: Dict[str, Tuple[str, Mapping[str, Any]]] = {}
    for raw_name, raw_cell in sorted(cells.items()):
        name = _name(raw_name, "AMF design cell")
        cell_type = raw_cell.get("type") if isinstance(raw_cell, Mapping) else None
        if cell_type in _CONSTANT_TYPES:
            continue
        if cell_type not in _FIXTURE_CELL_TYPES:
            raise ValidationError(
                f"AMF fixture adapter does not support cell type {cell_type!r}"
            )
        assert isinstance(raw_cell, Mapping)
        normalized[name] = (cell_type, raw_cell)
        for port, direction, bits in _cell_ports(raw_cell, f"cell {name!r}"):
            if direction != "output":
                continue
            for index, raw_bit in enumerate(bits):
                if raw_bit in constant_bits or raw_bit in {"0", "1"}:
                    raise ValidationError(
                        f"cell {name!r} output {port!r} drives a constant net"
                    )
                bit = constant_bits.get(raw_bit, raw_bit)
                if not isinstance(bit, int) or isinstance(bit, bool):
                    raise ValidationError(f"cell {name!r} output bit is invalid")
                refpin = port if len(bits) == 1 else f"{port}[{index}]"
                if bit in drivers:
                    raise ValidationError(f"mapped net bit {bit} has multiple drivers")
                drivers[bit] = f"{name}/{refpin}"

    lines: List[str] = []
    pin_count = 0
    for name, (cell_type, cell) in normalized.items():
        lines.append(f"curCell=> {name} type=> {cell_type}")
        for port, direction, bits in _cell_ports(cell, f"cell {name!r}"):
            for index, raw_bit in enumerate(bits):
                bit = constant_bits.get(raw_bit, raw_bit)
                refpin = port if len(bits) == 1 else f"{port}[{index}]"
                pin_name = f"{name}/{refpin}"
                if bit in {"0", "1"}:
                    net_name = f"<const{bit}>"
                    driver = net_name
                elif isinstance(bit, int) and not isinstance(bit, bool):
                    net_name = f"net_{bit}"
                    driver = pin_name if direction == "output" else drivers.get(bit)
                    if driver is None:
                        raise ValidationError(
                            f"cell {name!r} input {refpin!r} has no driver"
                        )
                else:
                    raise ValidationError(f"cell {name!r} port bit is invalid")
                lines.append(
                    f"   pin=> {pin_name} refpin=> {refpin} "
                    f"dir=> {'OUT' if direction == 'output' else 'IN'} "
                    f"net=> {net_name} drivepin=> {driver}"
                )
                pin_count += 1

    result = {
        "schema": AMF_DESIGN_ADAPTER_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "archive_text": "\n".join(lines) + ("\n" if lines else ""),
        "cells": list(normalized),
        "normalized_constant_cells": constants,
        "summary": {
            "cells": len(normalized),
            "pins": pin_count,
            "constant_cells": len(constants),
        },
    }
    return validate_amf_design(result)


def validate_amf_design(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != AMF_DESIGN_ADAPTER_SCHEMA or value.get("status") != "pass":
        raise ValidationError("AMF design adapter header is invalid")
    cells = value.get("cells")
    constants = value.get("normalized_constant_cells")
    text = value.get("archive_text")
    if (
        not isinstance(cells, list)
        or any(not isinstance(item, str) for item in cells)
        or len(cells) != len(set(cells))
        or not isinstance(constants, list)
        or any(not isinstance(item, str) for item in constants)
        or not isinstance(text, str)
    ):
        raise ValidationError("AMF design adapter payload is invalid")
    parsed_cells = [
        line.split()[1]
        for line in text.splitlines()
        if line.startswith("curCell=> ")
    ]
    if parsed_cells != cells:
        raise ValidationError("AMF design archive cell ownership is invalid")
    if any(f"curCell=> {name} " in text for name in constants):
        raise ValidationError("AMF design archive retained a constant pseudo-cell")
    return dict(value)


def _site_bels(
    architecture: ArchitectureDB, site: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    local = site.get("bels")
    if isinstance(local, list):
        return local
    template = site.get("template")
    templates = architecture.value.get("site_templates")
    if not isinstance(template, str) or not isinstance(templates, Mapping):
        raise ValidationError(f"AMF device site {site.get('name')!r} lacks BELs")
    contract = templates.get(template)
    if not isinstance(contract, Mapping) or not isinstance(contract.get("bels"), list):
        raise ValidationError(f"AMF device template {template!r} is invalid")
    return contract["bels"]


def export_amf_fixture_device(architecture_value: Mapping[str, Any]) -> Dict[str, Any]:
    """Serialize a single-SLR, single-clock-region fixture ArchitectureDB."""

    architecture = ArchitectureDB(architecture_value)
    if "xcvu19" in architecture.part.lower():
        raise ValidationError("AMF XCVU19P clock legality remains unverified")
    slrs = set()
    clock_regions = set()
    for site in architecture.value["sites"]:
        region = site.get("physical_region")
        if not isinstance(region, Mapping):
            raise ValidationError("AMF fixture device requires physical_region")
        slrs.add(region.get("slr"))
        clock_regions.add(region.get("clock_region"))
    if len(slrs) != 1 or None in slrs:
        raise ValidationError("AMF multi-SLR device adaptation is unsupported")
    if len(clock_regions) != 1 or None in clock_regions:
        raise ValidationError(
            "AMF fixture adapter requires exactly one clock region"
        )
    clock_region = next(iter(clock_regions))
    if not isinstance(clock_region, str) or _CLOCK_REGION_RE.fullmatch(clock_region) is None:
        raise ValidationError("AMF fixture clock region identity is invalid")

    lines: List[str] = []
    compatibility: Dict[str, Dict[str, List[str]]] = {}
    site_records: List[Dict[str, Any]] = []
    bel_count = 0
    for site in sorted(architecture.value["sites"], key=lambda item: item["name"]):
        site_name = _name(site.get("name"), "AMF device site")
        site_type = _name(site.get("type"), f"site {site_name!r} type")
        x, y = site.get("x"), site.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            raise ValidationError(f"site {site_name!r} coordinates are invalid")
        tile = site.get("tile")
        tile_name = f"AMF_FIXTURE_TILE_X{x}Y{y}"
        tile_type = "AMF_FIXTURE_TILE"
        if isinstance(tile, Mapping):
            tile_name = str(tile.get("name", tile_name))
            tile_type = str(tile.get("type", tile_type))
        bel_names = []
        for bel in _site_bels(architecture, site):
            bel_name = _name(bel.get("name"), f"site {site_name!r} BEL")
            compatible = bel.get("compatible_cells")
            if not isinstance(compatible, list) or not compatible:
                raise ValidationError(f"BEL {site_name}/{bel_name} is untyped")
            unsupported = sorted(set(compatible) - _FIXTURE_CELL_TYPES)
            if unsupported:
                raise ValidationError(
                    "AMF fixture device exposes unsupported cells: "
                    + ", ".join(unsupported)
                )
            bel_names.append(f"{site_name}/{bel_name}")
            bel_count += 1
            for cell_type in compatible:
                compatibility.setdefault(cell_type, {}).setdefault(
                    site_type, []
                ).append(bel_name)
        if not bel_names:
            raise ValidationError(f"site {site_name!r} has no AMF BELs")
        lines.append(
            f"site=> {site_name} tile=> {tile_name} "
            f"clockRegionName=> {clock_region} sitetype=> {site_type} "
            f"tiletype=> {tile_type} centerx=> {float(x):.6f} "
            f"centery=> {float(y):.6f} BELs=> [{','.join(bel_names)}]"
        )
        site_records.append({"name": site_name, "type": site_type})
    normalized_compatibility = {
        cell_type: {
            site_type: sorted(set(bels))
            for site_type, bels in sorted(site_types.items())
        }
        for cell_type, site_types in sorted(compatibility.items())
    }
    occupation_lines = []
    cell_to_shared_lines = []
    shared_to_bel_lines = []
    for cell_type, site_types in normalized_compatibility.items():
        occupation = 2 if cell_type in {"LUT6", "LUT6_2"} else 1
        occupation_lines.append(f"{cell_type} {occupation}")
        shared_types = [
            f"AMF_{site_type}_{cell_type}" for site_type in site_types
        ]
        cell_to_shared_lines.append(f"{cell_type} {','.join(shared_types)}")
        for site_type, shared_type in zip(site_types, shared_types):
            shared_to_bel_lines.append(
                f"{shared_type} {site_type} "
                + ",".join(site_types[site_type])
            )
    result = {
        "schema": AMF_DEVICE_ADAPTER_SCHEMA,
        "status": "pass",
        "part": architecture.part,
        "qualification_scope": "single-region-fixture-only",
        "archive_text": "\n".join(lines) + "\n",
        "sites": site_records,
        "compatibility": normalized_compatibility,
        "compatibility_text": {
            "cellType2fixedAmo": "\n".join(occupation_lines) + "\n",
            "cellType2sharedCellType": "\n".join(cell_to_shared_lines) + "\n",
            "sharedCellType2BELtype": "\n".join(shared_to_bel_lines) + "\n",
            "mergedSharedCellType2sharedCellType": "",
        },
        "summary": {
            "sites": len(site_records),
            "bels": bel_count,
            "slrs": 1,
            "clock_regions": 1,
        },
    }
    return validate_amf_fixture_device(result)


def validate_amf_fixture_device(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != AMF_DEVICE_ADAPTER_SCHEMA or value.get("status") != "pass":
        raise ValidationError("AMF device adapter header is invalid")
    if value.get("qualification_scope") != "single-region-fixture-only":
        raise ValidationError("AMF device adapter scope is invalid")
    sites = value.get("sites")
    compatibility = value.get("compatibility")
    compatibility_text = value.get("compatibility_text")
    text = value.get("archive_text")
    if not isinstance(sites, list) or not sites or not isinstance(compatibility, Mapping):
        raise ValidationError("AMF device adapter payload is invalid")
    expected_tables = {
        "cellType2fixedAmo",
        "cellType2sharedCellType",
        "sharedCellType2BELtype",
        "mergedSharedCellType2sharedCellType",
    }
    if (
        not isinstance(compatibility_text, Mapping)
        or set(compatibility_text) != expected_tables
        or any(not isinstance(item, str) for item in compatibility_text.values())
    ):
        raise ValidationError("AMF device compatibility tables are invalid")
    if not isinstance(text, str) or len(text.splitlines()) != len(sites):
        raise ValidationError("AMF device archive site count is invalid")
    return dict(value)


def parse_amf_place_cell_tcl(tcl: str) -> Dict[str, Tuple[str, str]]:
    """Parse AMF's generated placement records without executing Tcl."""

    if not isinstance(tcl, str):
        raise ValidationError("AMF result Tcl must be text")
    placements: Dict[str, Tuple[str, str]] = {}
    in_block = False
    saw_block = False
    for line_number, raw_line in enumerate(tcl.splitlines(), start=1):
        line = raw_line.strip()
        if not in_block:
            match = _PLACE_CELL_OPEN_RE.search(line)
            if match is None:
                continue
            saw_block = True
            in_block = True
            line = line[match.end():].strip()
        closes = "}" in line
        if closes:
            line = line.split("}", 1)[0].strip()
        if line:
            fields = line.split()
            if len(fields) != 2 or "/" not in fields[1]:
                raise ValidationError(
                    f"AMF result line {line_number} is not an exact cell/site/BEL record"
                )
            instance = _name(fields[0], f"AMF result line {line_number} instance")
            site, bel = fields[1].rsplit("/", 1)
            _name(site, f"AMF result line {line_number} site")
            _name(bel, f"AMF result line {line_number} BEL")
            if instance in placements:
                raise ValidationError(f"AMF result duplicates cell {instance!r}")
            placements[instance] = (site, bel)
        if closes:
            in_block = False
    if in_block or not saw_block:
        raise ValidationError("AMF result has an unterminated or missing place_cell block")
    return placements


def import_amf_fixture_result(
    tcl: str,
    *,
    mapped: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture_value: Mapping[str, Any],
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Import AMF records into fixed constraints plus an exact assignment seal."""

    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("AMF result mapped cells are invalid")
    if (
        packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA
        or packed.get("status") != "pass"
        or packed.get("top") != selected_top
        or not isinstance(packed.get("clusters"), list)
    ):
        raise ValidationError("AMF result PackedSiteNetlist identity is invalid")
    raw_constants = packed.get("unplaced_constants")
    if (
        not isinstance(raw_constants, list)
        or any(not isinstance(item, str) for item in raw_constants)
        or len(raw_constants) != len(set(raw_constants))
    ):
        raise ValidationError("AMF result PackedSiteNetlist constants are invalid")
    constants = set(raw_constants)
    if any(cells.get(item, {}).get("type") not in _CONSTANT_TYPES for item in constants):
        raise ValidationError("AMF result PackedSiteNetlist constants are invalid")

    raw_clusters = packed["clusters"]
    owners: Dict[str, str] = {}
    for cluster in raw_clusters:
        if (
            not isinstance(cluster, Mapping)
            or not isinstance(cluster.get("id"), str)
            or not isinstance(cluster.get("assignments"), list)
        ):
            raise ValidationError("AMF result cluster is invalid")
        for assignment in cluster["assignments"]:
            if not isinstance(assignment, Mapping):
                raise ValidationError("AMF result cluster assignment is invalid")
            instance = assignment.get("instance")
            cell_type = assignment.get("cell_type")
            if (
                not isinstance(instance, str)
                or cells.get(instance, {}).get("type") != cell_type
            ):
                raise ValidationError(
                    "AMF result cluster assignment differs from mapped netlist"
                )
            if instance in owners:
                raise ValidationError(
                    f"AMF result PackedSiteNetlist duplicates cell {instance!r}"
                )
            owners[instance] = cluster["id"]
    if set(owners).intersection(constants) or set(owners).union(constants) != set(cells):
        raise ValidationError("AMF result PackedSiteNetlist ownership is incomplete")

    architecture = ArchitectureDB(architecture_value)
    export_amf_fixture_device(architecture_value)
    placements = parse_amf_place_cell_tcl(tcl)
    expected_cells = set(owners)
    if set(placements) != expected_cells:
        missing = sorted(expected_cells - set(placements))
        extra = sorted(set(placements) - expected_cells)
        raise ValidationError(
            f"AMF result cell ownership differs: missing={missing}, extra={extra}"
        )

    constraints = []
    exact_assignments = []
    occupied_sites: Dict[str, str] = {}
    for cluster in sorted(raw_clusters, key=lambda item: item["id"]):
        cluster_id = cluster.get("id")
        assignments = cluster.get("assignments")
        if not isinstance(cluster_id, str) or not isinstance(assignments, list):
            raise ValidationError("AMF result cluster is invalid")
        site_names = {placements[item["instance"]][0] for item in assignments}
        if len(site_names) != 1:
            raise ValidationError(f"AMF split packed cluster {cluster_id!r}")
        site_name = next(iter(site_names))
        if site_name in occupied_sites:
            raise ValidationError(
                f"AMF result clusters {occupied_sites[site_name]!r} and "
                f"{cluster_id!r} overlap site {site_name!r}"
            )
        occupied_sites[site_name] = cluster_id
        site = architecture.site_named(site_name)
        if site is None:
            raise ValidationError(f"AMF result uses unknown site {site_name!r}")
        if site.get("type") not in cluster.get("site_templates", []):
            raise ValidationError(
                f"AMF result site {site_name!r} is incompatible with {cluster_id!r}"
            )
        bel_contract = {
            bel["name"]: set(bel.get("compatible_cells", []))
            for bel in _site_bels(architecture, site)
        }
        seen_bels = set()
        for assignment in assignments:
            instance = assignment["instance"]
            cell_type = assignment["cell_type"]
            _site, bel = placements[instance]
            allowed = assignment.get("bel_candidates") or [assignment.get("bel")]
            if bel not in allowed or cell_type not in bel_contract.get(bel, set()):
                raise ValidationError(
                    f"AMF result cell {instance!r} has illegal BEL {bel!r}"
                )
            if bel in seen_bels:
                raise ValidationError(f"AMF result reuses BEL {site_name}/{bel}")
            seen_bels.add(bel)
            exact_assignments.append(
                {
                    "cluster": cluster_id,
                    "instance": instance,
                    "cell_type": cell_type,
                    "site": site_name,
                    "bel": bel,
                }
            )
        constraints.append({"cluster": cluster_id, "site": site_name})

    only_slr = next(
        iter(
            {
                site["physical_region"]["slr"]
                for site in architecture.value["sites"]
            }
        )
    )
    result = {
        "schema": AMF_RESULT_ADAPTER_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "qualification_scope": "single-region-fixture-only",
        "constraints": {
            "schema": XILINX_CONSTRAINTS_SCHEMA,
            "global": {"allowed_slrs": [only_slr]},
            "clusters": constraints,
        },
        "assignments": exact_assignments,
        "summary": {
            "clusters": len(constraints),
            "cells": len(exact_assignments),
            "site_types": dict(
                sorted(Counter(
                    architecture.site_named(item["site"])["type"]
                    for item in exact_assignments
                ).items())
            ),
        },
    }
    return validate_amf_fixture_result(result)


def validate_amf_fixture_result(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != AMF_RESULT_ADAPTER_SCHEMA or value.get("status") != "pass":
        raise ValidationError("AMF result adapter header is invalid")
    if value.get("qualification_scope") != "single-region-fixture-only":
        raise ValidationError("AMF result adapter scope is invalid")
    constraints = value.get("constraints")
    assignments = value.get("assignments")
    if (
        not isinstance(constraints, Mapping)
        or constraints.get("schema") != XILINX_CONSTRAINTS_SCHEMA
        or not isinstance(assignments, list)
        or not assignments
    ):
        raise ValidationError("AMF result adapter payload is invalid")
    instances = [item.get("instance") for item in assignments if isinstance(item, Mapping)]
    if len(instances) != len(assignments) or len(instances) != len(set(instances)):
        raise ValidationError("AMF result adapter assignment ownership is invalid")
    return dict(value)


def validate_amf_fixture_roundtrip(
    *,
    mapped: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture_value: Mapping[str, Any],
    result_tcl: str,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Exercise all three adapters for one in-memory fixture."""

    design = export_amf_design(mapped, top=top)
    device = export_amf_fixture_device(architecture_value)
    result = import_amf_fixture_result(
        result_tcl,
        mapped=mapped,
        packed=packed,
        architecture_value=architecture_value,
        top=top,
    )
    return {
        "schema": AMF_FIXTURE_ROUNDTRIP_SCHEMA,
        "status": "pass",
        "qualification_scope": "single-region-fixture-only",
        "summary": {
            "design_cells": design["summary"]["cells"],
            "normalized_constant_cells": design["summary"]["constant_cells"],
            "device_sites": device["summary"]["sites"],
            "result_cells": result["summary"]["cells"],
        },
        "result_constraints": result["constraints"],
        "result_assignments": result["assignments"],
    }
