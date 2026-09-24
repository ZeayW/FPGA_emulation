"""Minimal FPGA Interchange adapters for the DREAMPlaceFPGA candidate.

The supported subset is intentionally narrow: LUT1--LUT6/LUT6_2, FDRE,
DSP48E2, and RAMB36E2.  The adapters never lower or approximate unsupported
primitives.  Their output is a candidate certificate, not a production Phase 7
placement, because pinned DREAMPlaceFPGA lacks required detailed placement and
several Route-A constraints.
"""

from __future__ import annotations

import gzip
import importlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_primitives import audit_xilinx_mapped_json


DREAMPLACEFPGA_LOGICAL_MODEL_SCHEMA = (
    "emuflow.dreamplacefpga-logical-model/v1"
)
DREAMPLACEFPGA_PLACEMENT_CANDIDATE_SCHEMA = (
    "emuflow.dreamplacefpga-placement-candidate/v1"
)
DREAMPLACEFPGA_INTERCHANGE_PROVIDER = (
    "dreamplacefpga-interchange-candidate-v1"
)
DREAMPLACEFPGA_QUALIFICATION_BOUNDARY = (
    "fixture identity/site/BEL certificate only; detailed placement "
    "and full Route-A constraint support remain missing"
)
DEFAULT_INTERCHANGE_SCHEMA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "engines"
    / "fpga-interchange-schema"
    / "interchange"
)

DREAMPLACEFPGA_NATIVE_FIXTURE_PRIMITIVES = frozenset({
    "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "LUT6_2",
    "FDRE", "DSP48E2", "RAMB36E2",
})


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _select_top(
    source: Mapping[str, Any], top: Optional[str]
) -> tuple[str, Mapping[str, Any]]:
    modules = source.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        return top, module
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        return name, module
    marked = [
        name
        for name, module in modules.items()
        if isinstance(module, dict)
        and str(module.get("attributes", {}).get("top", "0")) not in {"", "0"}
    ]
    if len(marked) != 1:
        raise ValidationError("mapped Yosys JSON top is ambiguous")
    return marked[0], modules[marked[0]]


def _port_contract(
    directions: object,
    connections: object,
    *,
    context: str,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Sequence[Any]]]:
    if not isinstance(directions, dict) or not isinstance(connections, dict):
        raise ValidationError(f"{context} lacks explicit port metadata")
    if set(directions) != set(connections):
        raise ValidationError(f"{context} port metadata is incomplete")
    ports: Dict[str, Dict[str, Any]] = {}
    checked_connections: Dict[str, Sequence[Any]] = {}
    for name in sorted(directions):
        direction = directions[name]
        bits = connections[name]
        if direction not in {"input", "output"}:
            raise ValidationError(f"{context}.{name} has unsupported direction")
        if not isinstance(bits, list) or not bits:
            raise ValidationError(f"{context}.{name} has invalid connections")
        if any(not isinstance(bit, int) for bit in bits):
            raise ValidationError(
                f"{context}.{name} contains a constant or unknown bit; "
                "the candidate adapter does not approximate constants"
            )
        ports[name] = {"name": name, "direction": direction, "width": len(bits)}
        checked_connections[name] = bits
    return ports, checked_connections


def _property_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def build_dreamplacefpga_logical_model(
    mapped_json: Path, *, top: Optional[str] = None
) -> Dict[str, Any]:
    """Convert mapped JSON into a deterministic logical Interchange model."""

    audit = audit_xilinx_mapped_json(mapped_json, top=top)
    unsupported = sorted(
        set(audit["cell_types"]) - DREAMPLACEFPGA_NATIVE_FIXTURE_PRIMITIVES
    )
    if unsupported:
        raise ValidationError(
            "DREAMPlaceFPGA IF adapter does not support mapped primitives: "
            + ", ".join(unsupported)
        )
    source = read_json(mapped_json)
    selected_top, module = _select_top(source, top)
    cells = module.get("cells")
    top_ports = module.get("ports", {})
    netnames = module.get("netnames", {})
    if not isinstance(cells, dict) or not isinstance(top_ports, dict):
        raise ValidationError("mapped Yosys JSON body is invalid")

    declarations: Dict[str, Dict[str, Dict[str, Any]]] = {}
    instances = []
    endpoints: Dict[int, list[Dict[str, Any]]] = defaultdict(list)
    drivers: Counter[int] = Counter()
    for instance, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            raise ValidationError(f"mapped cell {instance!r} is invalid")
        cell_type = cell.get("type")
        if cell_type not in DREAMPLACEFPGA_NATIVE_FIXTURE_PRIMITIVES:
            raise ValidationError(
                f"DREAMPlaceFPGA IF adapter does not support {cell_type!r}"
            )
        ports, connections = _port_contract(
            cell.get("port_directions"),
            cell.get("connections"),
            context=f"mapped cell {instance!r}",
        )
        previous = declarations.setdefault(cell_type, ports)
        if previous != ports:
            raise ValidationError(
                f"mapped cell type {cell_type!r} has inconsistent port declarations"
            )
        parameters = cell.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValidationError(f"mapped cell {instance!r} parameters are invalid")
        instances.append({
            "name": instance,
            "type": cell_type,
            "properties": {
                str(name): _property_text(value)
                for name, value in sorted(parameters.items())
            },
        })
        for port, bits in connections.items():
            direction = ports[port]["direction"]
            for bit_index, signal in enumerate(bits):
                endpoints[signal].append({
                    "instance": instance,
                    "port": port,
                    "bit": bit_index,
                })
                if direction == "output":
                    drivers[signal] += 1

    checked_top_ports = []
    for name, port in sorted(top_ports.items()):
        if not isinstance(port, dict):
            raise ValidationError(f"mapped top port {name!r} is invalid")
        direction = port.get("direction")
        bits = port.get("bits")
        if direction not in {"input", "output"} or not isinstance(bits, list):
            raise ValidationError(f"mapped top port {name!r} is invalid")
        checked_top_ports.append({
            "name": name, "direction": direction, "width": len(bits)
        })
        for bit_index, signal in enumerate(bits):
            if signal == "x":
                continue
            if not isinstance(signal, int):
                raise ValidationError(
                    f"mapped top port {name!r} contains unsupported bit {signal!r}"
                )
            endpoints[signal].append({
                "instance": None,
                "port": name,
                "bit": bit_index,
            })
            if direction == "input":
                drivers[signal] += 1

    names_by_bit: Dict[int, list[str]] = defaultdict(list)
    if isinstance(netnames, dict):
        for name, value in sorted(netnames.items()):
            bits = value.get("bits") if isinstance(value, dict) else None
            if isinstance(bits, list):
                for bit_index, bit in enumerate(bits):
                    if isinstance(bit, int):
                        names_by_bit[bit].append(
                            name if len(bits) == 1 else f"{name}[{bit_index}]"
                        )
    nets = []
    for signal in sorted(endpoints):
        if drivers[signal] > 1:
            raise ValidationError(f"mapped signal {signal} has multiple drivers")
        names = names_by_bit.get(signal, [])
        nets.append({
            "name": names[0] if names else f"$signal${signal}",
            "signal": signal,
            "endpoints": sorted(
                endpoints[signal],
                key=lambda item: (
                    item["instance"] is None,
                    item["instance"] or "",
                    item["port"],
                    item["bit"],
                ),
            ),
        })
    return {
        "schema": DREAMPLACEFPGA_LOGICAL_MODEL_SCHEMA,
        "top": selected_top,
        "library": "emuflow",
        "view": "netlist",
        "declarations": [
            {"name": cell_type, "ports": list(ports.values())}
            for cell_type, ports in sorted(declarations.items())
        ],
        "top_ports": checked_top_ports,
        "instances": instances,
        "nets": nets,
        "source": {
            "mapped_sha256": _sha256(mapped_json),
            "primitive_profile": audit["mapping_profile"],
        },
    }


def _load_capnp_schema(schema_root: Path, filename: str) -> Any:
    try:
        capnp = importlib.import_module("capnp")
    except ImportError as error:
        raise ValidationError(
            "FPGA Interchange serialization requires the optional pycapnp runtime"
        ) from error
    schema_path = schema_root / filename
    if not schema_path.is_file():
        raise ValidationError(f"FPGA Interchange schema is missing: {schema_path}")
    import_roots = [str(schema_root), str(Path(capnp.__file__).resolve().parent.parent)]
    try:
        return capnp.load(str(schema_path), imports=import_roots)
    except Exception as error:
        raise ValidationError(
            f"cannot load FPGA Interchange schema {schema_path}"
        ) from error


def _collect_strings(model: Mapping[str, Any]) -> tuple[list[str], Dict[str, int]]:
    values = {model["top"], model["library"], model["view"]}
    for declaration in model["declarations"]:
        values.add(declaration["name"])
        values.update(port["name"] for port in declaration["ports"])
    values.update(port["name"] for port in model["top_ports"])
    for instance in model["instances"]:
        values.update((instance["name"], instance["type"]))
        values.update(instance["properties"])
        values.update(instance["properties"].values())
    values.update(net["name"] for net in model["nets"])
    strings = sorted(values)
    return strings, {value: index for index, value in enumerate(strings)}


def _init_empty_properties(value: Any) -> None:
    value.propMap.init("entries", 0)


def write_dreamplacefpga_logical_netlist(
    mapped_json: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
    schema_root: Path = DEFAULT_INTERCHANGE_SCHEMA_ROOT,
) -> Dict[str, Any]:
    """Write a gzip-wrapped official FPGA Interchange LogicalNetlist."""

    model = build_dreamplacefpga_logical_model(mapped_json, top=top)
    schema = _load_capnp_schema(schema_root, "LogicalNetlist.capnp")
    strings, string_index = _collect_strings(model)
    declarations = list(model["declarations"]) + [{
        "name": model["top"], "ports": model["top_ports"]
    }]
    declaration_index = {
        declaration["name"]: index
        for index, declaration in enumerate(declarations)
    }
    port_indices: Dict[tuple[str, str], tuple[int, int]] = {}
    next_port = 0
    for declaration in declarations:
        for port in declaration["ports"]:
            port_indices[(declaration["name"], port["name"])] = (
                next_port, port["width"]
            )
            next_port += 1

    message = schema.Netlist.new_message()
    message.name = model["top"]
    _init_empty_properties(message)
    string_list = message.init("strList", len(strings))
    for index, value in enumerate(strings):
        string_list[index] = value
    ports = message.init("portList", next_port)
    for declaration in declarations:
        for port in declaration["ports"]:
            index, _width = port_indices[(declaration["name"], port["name"])]
            entry = ports[index]
            entry.name = string_index[port["name"]]
            entry.dir = port["direction"]
            _init_empty_properties(entry)
            if port["width"] == 1:
                entry.bit = None
            else:
                entry.bus.busStart = 0
                entry.bus.busEnd = port["width"] - 1

    cell_declarations = message.init("cellDecls", len(declarations))
    for index, declaration in enumerate(declarations):
        entry = cell_declarations[index]
        entry.name = string_index[declaration["name"]]
        entry.view = string_index[model["view"]]
        entry.lib = string_index[model["library"]]
        _init_empty_properties(entry)
        port_refs = entry.init("ports", len(declaration["ports"]))
        for port_index, port in enumerate(declaration["ports"]):
            port_refs[port_index] = port_indices[
                (declaration["name"], port["name"])
            ][0]

    top_index = declaration_index[model["top"]]
    message.topInst.name = string_index[model["top"]]
    message.topInst.view = string_index[model["view"]]
    message.topInst.cell = top_index
    _init_empty_properties(message.topInst)
    instance_index = {
        instance["name"]: index
        for index, instance in enumerate(model["instances"])
    }
    instances = message.init("instList", len(model["instances"]))
    for index, instance in enumerate(model["instances"]):
        entry = instances[index]
        entry.name = string_index[instance["name"]]
        entry.view = string_index[model["view"]]
        entry.cell = declaration_index[instance["type"]]
        properties = entry.propMap.init(
            "entries", len(instance["properties"])
        )
        for property_index, (name, value) in enumerate(
            sorted(instance["properties"].items())
        ):
            properties[property_index].key = string_index[name]
            properties[property_index].textValue = string_index[value]

    cell_list = message.init("cellList", 1)
    cell_list[0].index = top_index
    instance_refs = cell_list[0].init("insts", len(model["instances"]))
    for index in range(len(model["instances"])):
        instance_refs[index] = index
    nets = cell_list[0].init("nets", len(model["nets"]))
    instance_types = {
        instance["name"]: instance["type"] for instance in model["instances"]
    }
    for index, net in enumerate(model["nets"]):
        entry = nets[index]
        entry.name = string_index[net["name"]]
        _init_empty_properties(entry)
        endpoint_list = entry.init("portInsts", len(net["endpoints"]))
        for endpoint_index, endpoint in enumerate(net["endpoints"]):
            endpoint_entry = endpoint_list[endpoint_index]
            cell_type = (
                model["top"]
                if endpoint["instance"] is None
                else instance_types[endpoint["instance"]]
            )
            port_index, width = port_indices[(cell_type, endpoint["port"])]
            endpoint_entry.port = port_index
            if width == 1:
                endpoint_entry.busIdx.singleBit = None
            else:
                endpoint_entry.busIdx.idx = endpoint["bit"]
            if endpoint["instance"] is None:
                endpoint_entry.extPort = None
            else:
                endpoint_entry.inst = instance_index[endpoint["instance"]]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wb") as stream:
        stream.write(message.to_bytes())
    return {
        "status": "pass",
        "schema": "emuflow.dreamplacefpga-logical-netlist-export/v1",
        "provider": DREAMPLACEFPGA_INTERCHANGE_PROVIDER,
        "top": model["top"],
        "instances": len(model["instances"]),
        "nets": len(model["nets"]),
        "output": str(output_path),
        "output_sha256": _sha256(output_path),
        "qualification_boundary": "candidate fixture input only",
    }


def read_dreamplacefpga_physical_placements(
    physical_netlist: Path,
    *,
    schema_root: Path = DEFAULT_INTERCHANGE_SCHEMA_ROOT,
) -> Dict[str, Any]:
    """Read placement records from an official gzip-wrapped `.phys` file."""

    schema = _load_capnp_schema(schema_root, "PhysicalNetlist.capnp")
    try:
        with gzip.open(physical_netlist, "rb") as stream:
            payload = stream.read()
    except OSError as error:
        raise ValidationError("DREAMPlaceFPGA .phys is not valid gzip") from error
    if not payload:
        raise ValidationError("DREAMPlaceFPGA .phys payload is empty")
    try:
        reader = schema.PhysNetlist.from_bytes(
            payload, traversal_limit_in_words=2**31
        )
        with reader as value:
            strings = list(value.strList)

            def string_at(index: int, context: str) -> str:
                if index < 0 or index >= len(strings) or not strings[index]:
                    raise ValidationError(f"{context} has invalid string reference")
                return strings[index]

            placements = []
            for index, placement in enumerate(value.placements):
                context = f"physical placements[{index}]"
                placements.append({
                    "instance": string_at(placement.cellName, context),
                    "cell_type": string_at(placement.type, context),
                    "site": string_at(placement.site, context),
                    "bel": string_at(placement.bel, context),
                    "site_fixed": bool(placement.isSiteFixed),
                    "bel_fixed": bool(placement.isBelFixed),
                })
            return {"part": str(value.part), "placements": placements}
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError("cannot decode DREAMPlaceFPGA .phys output") from error


def _site_bel_contract(
    architecture: ArchitectureDB, site_name: str, bel_name: str, cell_type: str
) -> Mapping[str, Any]:
    site = architecture.site_named(site_name)
    if site is None:
        raise ValidationError(f"DREAMPlaceFPGA placed a cell at unknown site {site_name!r}")
    template_name = site.get("template", site.get("type"))
    template = architecture.value.get("site_templates", {}).get(template_name)
    if not isinstance(template, dict):
        raise ValidationError(f"site {site_name!r} has no template contract")
    for bel in template.get("bels", []):
        if bel.get("name") != bel_name:
            continue
        compatible = bel.get("compatible_cells", [])
        if cell_type not in compatible and bel.get("type") != cell_type:
            raise ValidationError(
                f"cell {cell_type!r} is incompatible with {site_name}/{bel_name}"
            )
        return site
    raise ValidationError(f"site {site_name!r} has no BEL {bel_name!r}")


def _mapped_cell_inventory(
    mapped_json: Path, *, top: Optional[str]
) -> tuple[str, Dict[str, str]]:
    model = build_dreamplacefpga_logical_model(mapped_json, top=top)
    return model["top"], {
        instance["name"]: instance["type"] for instance in model["instances"]
    }


def build_dreamplacefpga_placement_candidate(
    mapped_json: Path,
    architecture_path: Path,
    physical_netlist: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
    decoded: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a checked diagnostic placement certificate from `.phys`."""

    selected_top, mapped_cells = _mapped_cell_inventory(mapped_json, top=top)
    architecture = ArchitectureDB.load(architecture_path)
    physical = (
        dict(decoded)
        if decoded is not None
        else read_dreamplacefpga_physical_placements(physical_netlist)
    )
    if physical.get("part") != architecture.part:
        raise ValidationError("DREAMPlaceFPGA .phys part does not match ArchitectureDB")
    records = physical.get("placements")
    if not isinstance(records, list):
        raise ValidationError("DREAMPlaceFPGA placement population is invalid")
    observed: Dict[str, Mapping[str, Any]] = {}
    occupied = set()
    by_site: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationError(f"DREAMPlaceFPGA placement[{index}] is invalid")
        instance = record.get("instance")
        cell_type = record.get("cell_type")
        site_name = record.get("site")
        bel_name = record.get("bel")
        if instance not in mapped_cells or instance in observed:
            raise ValidationError("DREAMPlaceFPGA placement ownership is invalid")
        if cell_type != mapped_cells[instance]:
            raise ValidationError(
                f"DREAMPlaceFPGA changed the type of cell {instance!r}"
            )
        if not isinstance(site_name, str) or not isinstance(bel_name, str):
            raise ValidationError("DREAMPlaceFPGA site/BEL assignment is invalid")
        if (site_name, bel_name) in occupied:
            raise ValidationError(
                f"DREAMPlaceFPGA overlaps BEL {site_name}/{bel_name}"
            )
        site = _site_bel_contract(
            architecture, site_name, bel_name, str(cell_type)
        )
        occupied.add((site_name, bel_name))
        observed[str(instance)] = record
        by_site[site_name].append({
            "instance": instance,
            "cell_type": cell_type,
            "bel": bel_name,
            "site_fixed": bool(record.get("site_fixed")),
            "bel_fixed": bool(record.get("bel_fixed")),
            "placement_mode": "dreamplacefpga-interchange-candidate",
            "site": site_name,
        })
    if set(observed) != set(mapped_cells):
        missing = sorted(set(mapped_cells) - set(observed))
        raise ValidationError(
            "DREAMPlaceFPGA placement coverage is incomplete: " + ", ".join(missing)
        )

    clusters = []
    for site_name, assignments in sorted(by_site.items()):
        site = architecture.site_named(site_name)
        assert site is not None
        clusters.append({
            "cluster": f"dreamplacefpga:{site_name}",
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "fixed": all(
                bool(observed[item["instance"]].get("site_fixed"))
                for item in assignments
            ),
            "physical_region": site.get("physical_region"),
            "assignments": sorted(assignments, key=lambda item: item["instance"]),
        })
    result = {
        "schema": DREAMPLACEFPGA_PLACEMENT_CANDIDATE_SCHEMA,
        "status": "pass",
        "provider": DREAMPLACEFPGA_INTERCHANGE_PROVIDER,
        "part": architecture.part,
        "top": selected_top,
        "source": {
            "mapped_sha256": _sha256(mapped_json),
            "architecture_sha256": _sha256(architecture_path),
            "physical_netlist_sha256": _sha256(physical_netlist),
        },
        "clusters": clusters,
        "summary": {
            "cells": len(observed),
            "sites": len(clusters),
            "cell_types": dict(sorted(Counter(mapped_cells.values()).items())),
        },
        "production_qualified": False,
        "qualification_boundary": DREAMPLACEFPGA_QUALIFICATION_BOUNDARY,
    }
    write_json(output_path, result, compact=True)
    validate_dreamplacefpga_placement_candidate(
        mapped_json, architecture_path, physical_netlist, output_path, top=top
    )
    return result


def validate_dreamplacefpga_placement_candidate(
    mapped_json: Path,
    architecture_path: Path,
    physical_netlist: Path,
    placement_path: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Independently validate the saved candidate certificate."""

    value = read_json(placement_path)
    if not isinstance(value, dict) or value.get("schema") != (
        DREAMPLACEFPGA_PLACEMENT_CANDIDATE_SCHEMA
    ):
        raise ValidationError("DREAMPlaceFPGA placement candidate schema is invalid")
    if (
        value.get("status") != "pass"
        or value.get("provider") != DREAMPLACEFPGA_INTERCHANGE_PROVIDER
        or value.get("production_qualified") is not False
        or value.get("qualification_boundary")
        != DREAMPLACEFPGA_QUALIFICATION_BOUNDARY
    ):
        raise ValidationError("DREAMPlaceFPGA placement candidate identity is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    selected_top, mapped_cells = _mapped_cell_inventory(mapped_json, top=top)
    if value.get("part") != architecture.part or value.get("top") != selected_top:
        raise ValidationError("DREAMPlaceFPGA placement candidate target is invalid")
    source = value.get("source")
    expected_source = {
        "mapped_sha256": _sha256(mapped_json),
        "architecture_sha256": _sha256(architecture_path),
        "physical_netlist_sha256": _sha256(physical_netlist),
    }
    if source != expected_source:
        raise ValidationError("DREAMPlaceFPGA placement candidate source is invalid")
    clusters = value.get("clusters")
    if not isinstance(clusters, list):
        raise ValidationError("DREAMPlaceFPGA placement candidate clusters are invalid")
    seen_cells = set()
    occupied = set()
    for cluster in clusters:
        if not isinstance(cluster, dict):
            raise ValidationError("DREAMPlaceFPGA placement cluster is invalid")
        site_name = cluster.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None:
            raise ValidationError("DREAMPlaceFPGA placement site is invalid")
        if (
            cluster.get("cluster") != f"dreamplacefpga:{site_name}"
            or cluster.get("site_type") != site["type"]
            or (cluster.get("x"), cluster.get("y")) != (site["x"], site["y"])
            or cluster.get("physical_region") != site.get("physical_region")
        ):
            raise ValidationError("DREAMPlaceFPGA placement site contract is invalid")
        assignments = cluster.get("assignments")
        if (
            not isinstance(assignments, list)
            or not assignments
            or any(not isinstance(item, dict) for item in assignments)
        ):
            raise ValidationError("DREAMPlaceFPGA placement assignments are invalid")
        if cluster.get("fixed") is not all(
            bool(item.get("site_fixed"))
            for item in assignments
        ):
            raise ValidationError("DREAMPlaceFPGA placement fixed state is invalid")
        for assignment in assignments:
            instance = assignment.get("instance")
            cell_type = assignment.get("cell_type")
            bel_name = assignment.get("bel")
            if (
                instance not in mapped_cells
                or instance in seen_cells
                or cell_type != mapped_cells[instance]
                or assignment.get("site") != site_name
                or assignment.get("placement_mode")
                != "dreamplacefpga-interchange-candidate"
            ):
                raise ValidationError("DREAMPlaceFPGA cell assignment is invalid")
            if (site_name, bel_name) in occupied:
                raise ValidationError("DREAMPlaceFPGA placement has a BEL overlap")
            _site_bel_contract(
                architecture, site_name, str(bel_name), str(cell_type)
            )
            occupied.add((site_name, bel_name))
            seen_cells.add(instance)
    if seen_cells != set(mapped_cells):
        raise ValidationError("DREAMPlaceFPGA placement cell coverage is incomplete")
    expected_summary = {
        "cells": len(seen_cells),
        "sites": len(clusters),
        "cell_types": dict(sorted(Counter(mapped_cells.values()).items())),
    }
    if value.get("summary") != expected_summary:
        raise ValidationError("DREAMPlaceFPGA placement summary is invalid")
    physical = read_dreamplacefpga_physical_placements(physical_netlist)
    if physical.get("part") != architecture.part:
        raise ValidationError("DREAMPlaceFPGA physical target is invalid")
    physical_records = physical.get("placements")
    if not isinstance(physical_records, list):
        raise ValidationError("DREAMPlaceFPGA physical placement population is invalid")
    physical_cells = {}
    for record in physical_records:
        if not isinstance(record, dict) or record.get("instance") in physical_cells:
            raise ValidationError("DREAMPlaceFPGA physical cell ownership is invalid")
        physical_cells[record.get("instance")] = (
            record.get("cell_type"), record.get("site"), record.get("bel"),
            bool(record.get("site_fixed")), bool(record.get("bel_fixed")),
        )
    certificate_cells = {
        assignment["instance"]: (
            assignment["cell_type"], assignment["site"], assignment["bel"],
            assignment.get("site_fixed"), assignment.get("bel_fixed"),
        )
        for cluster in clusters
        for assignment in cluster["assignments"]
    }
    if physical_cells != certificate_cells:
        raise ValidationError(
            "DREAMPlaceFPGA placement certificate disagrees with .phys"
        )
    return {
        "status": "pass",
        "schema": "emuflow.dreamplacefpga-placement-candidate-validation/v1",
        "cells": len(seen_cells),
        "sites": len(clusters),
        "production_qualified": False,
    }
