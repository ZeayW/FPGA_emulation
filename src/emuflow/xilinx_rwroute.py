"""Compact RWRoute adapter and independent route-certificate checker."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_PLACEMENT_SCHEMA


XILINX_ROUTE_DB_SCHEMA = "emuflow.xilinx-route-db/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_module(mapped: Mapping[str, Any], top: str) -> Mapping[str, Any]:
    modules = mapped.get("modules")
    module = modules.get(top) if isinstance(modules, dict) else None
    if not isinstance(module, dict) or not isinstance(module.get("cells"), dict):
        raise ValidationError(f"mapped JSON has no valid top module {top!r}")
    return module


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def export_rwroute_input(
    mapped_path: Path,
    packed_path: Path,
    placement_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    mapped, packed, placement = (
        read_json(mapped_path), read_json(packed_path), read_json(placement_path)
    )
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx placement header is invalid")
    top = packed.get("top")
    if not isinstance(top, str):
        raise ValidationError("PackedSiteNetlist top is invalid")
    cells = _select_module(mapped, top)["cells"]
    physical = {
        assignment["instance"]: (
            assignment.get("site", entry["site"]), assignment["bel"]
        )
        for entry in placement["clusters"]
        for assignment in entry["assignments"]
    }
    if set(physical) != {
        assignment["instance"]
        for cluster in packed["clusters"]
        for assignment in cluster["assignments"]
    }:
        raise ValidationError("placement and packing cell ownership disagree")
    # RapidWright models LUT6_2 as a transformed primitive and deliberately
    # rejects direct createAndPlaceCell(LUT6_2).  Preserve one logical/resource
    # cell in EmuFlow, but lower it at this provider boundary to the documented
    # shared A5LUT/A6LUT physical pair.  Only the four functional route-through
    # pins may carry non-constant nets.
    route_cells: Dict[str, Tuple[str, str, str, str]] = {}
    pin_bindings: Dict[Tuple[str, str], Tuple[str, str]] = {}
    expanded_lut6_2 = 0
    for name in sorted(physical):
        site, bel = physical[name]
        cell_type = cells[name]["type"]
        if cell_type != "LUT6_2":
            route_cells[name] = (name, cell_type, site, bel)
            continue
        match = re.fullmatch(r"([A-H])6LUT", bel)
        if match is None:
            raise ValidationError(
                f"LUT6_2 cell {name!r} is not assigned to a 6LUT BEL"
            )
        letter = match.group(1)
        o5_key = f"{name}\0O5"
        o6_key = f"{name}\0O6"
        route_cells[o5_key] = (f"{name}$physical_o5", "LUT5", site, f"{letter}5LUT")
        route_cells[o6_key] = (f"{name}$physical_o6", "LUT6", site, f"{letter}6LUT")
        pin_bindings[(name, "I0")] = (o5_key, "I0")
        pin_bindings[(name, "O5")] = (o5_key, "O")
        pin_bindings[(name, "I1")] = (o6_key, "I0")
        pin_bindings[(name, "O6")] = (o6_key, "O")
        expanded_lut6_2 += 1
    safe = {
        name: f"c{index}" for index, name in enumerate(sorted(route_cells))
    }
    lines = [
        f"META\tpart\t{placement['part']}",
        f"META\tmapped_sha256\t{_sha256(mapped_path)}",
        f"META\tpacked_sha256\t{_sha256(packed_path)}",
        f"META\tplacement_sha256\t{_sha256(placement_path)}",
    ]
    for route_name in sorted(route_cells):
        display_name, cell_type, site, bel = route_cells[route_name]
        if any(character in display_name for character in "\t\r\n"):
            raise ValidationError(f"cell name {display_name!r} is not TSV-safe")
        lines.append(
            f"CELL\t{safe[route_name]}\t{display_name}\t{cell_type}\t{site}\t{bel}"
        )

    endpoints: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for name in sorted(physical):
        cell = cells[name]
        directions = cell.get("port_directions", {})
        for port, bits in cell.get("connections", {}).items():
            if not isinstance(bits, list):
                raise ValidationError(f"cell {name!r} port {port!r} is invalid")
            direction = directions.get(port)
            if direction not in {"input", "output", "inout"}:
                raise ValidationError(f"cell {name!r} port direction is invalid")
            role = "driver" if direction in {"output", "inout"} else "sink"
            for index, bit in enumerate(bits):
                if isinstance(bit, int):
                    endpoints[bit].append(
                        (name, _logical_pin(port, index, len(bits)), role)
                    )

    included = 0
    excluded = 0
    for bit in sorted(endpoints):
        values = endpoints[bit]
        drivers = [value for value in values if value[2] == "driver"]
        sinks = [value for value in values if value[2] == "sink"]
        net_name = f"n{bit}"
        if len(drivers) != 1 or not sinks:
            reason = "boundary_or_driverless" if not drivers else "multiple_driver_or_sinkless"
            lines.append(f"EXCLUDED\t{net_name}\t{reason}")
            excluded += 1
            continue
        sites = {physical[name][0] for name, _pin, _role in [drivers[0], *sinks]}
        if len(sites) == 1:
            lines.append(f"EXCLUDED\t{net_name}\tintra_site")
            excluded += 1
            continue
        kind = "clock" if any(pin in {"C", "CLK"} for _cell, pin, _role in sinks) else "signal"
        lines.append(f"NET\t{net_name}\t{kind}")
        for name, pin, role in [drivers[0], *sinks]:
            route_name, route_pin = pin_bindings.get(
                (name, pin), (name, pin)
            )
            if route_name not in route_cells:
                raise ValidationError(
                    f"cell {name!r} pin {pin!r} has no RWRoute physical binding"
                )
            lines.append(
                f"PIN\t{net_name}\t{safe[route_name]}\t{route_pin}\t{role}"
            )
        included += 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "pass", "cells": len(safe), "logical_cells": len(physical),
        "expanded_lut6_2_cells": expanded_lut6_2,
        "routable_nets": included,
        "excluded_nets": excluded, "output": str(output_path),
    }


def run_rwroute(
    input_path: Path,
    output_path: Path,
    *,
    rapidwright_jar: Path,
    java: Path,
    classes_dir: Path,
    java_source: Path,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    classes_dir.mkdir(parents=True, exist_ok=True)
    runtime_home = classes_dir.parent / "rapidwright-runtime-home"
    runtime_home.mkdir(parents=True, exist_ok=True)
    rapidwright_path = runtime_home / "RapidWright"
    rapidwright_path.mkdir(parents=True, exist_ok=True)
    class_file = classes_dir / "EmuFlowRWRoute.class"
    if not class_file.is_file() or class_file.stat().st_mtime < java_source.stat().st_mtime:
        javac = java.with_name("javac")
        completed = subprocess.run(
            [str(javac), "-cp", str(rapidwright_jar), "-d", str(classes_dir), str(java_source)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        if completed.returncode != 0:
            raise ValidationError("RWRoute adapter compilation failed:\n" + completed.stdout[-8000:])
    command = [
        str(java), "-Xmx32g", f"-Duser.home={runtime_home}",
        "-cp", f"{classes_dir}:{rapidwright_jar}",
        "EmuFlowRWRoute", str(input_path), str(output_path),
    ]
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False,
        env={**os.environ, "RAPIDWRIGHT_PATH": str(rapidwright_path)},
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise ValidationError(
            f"RWRoute failed with exit code {completed.returncode}:\n"
            + "\n".join(completed.stdout.splitlines()[-80:])
        )
    report = validate_xilinx_route_db(output_path)
    return {**report, "output": str(output_path), "log": str(log_path) if log_path else None}


def validate_xilinx_route_db(
    path: Path,
    *,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
) -> Dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_ROUTE_DB_SCHEMA:
        raise ValidationError("XilinxRouteDB header is invalid")
    if value.get("status") not in {"candidate", "pass"}:
        raise ValidationError("XilinxRouteDB status is invalid")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValidationError("XilinxRouteDB source seal is missing")
    expected_sources = {
        "mapped_sha256": mapped_path,
        "packed_sha256": packed_path,
        "placement_sha256": placement_path,
    }
    for key, source_path in expected_sources.items():
        digest = source.get(key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"XilinxRouteDB source.{key} is invalid")
        if source_path is not None and digest != _sha256(source_path):
            raise ValidationError(f"XilinxRouteDB source.{key} does not match input")
    input_digest = source.get("rwroute_input_sha256")
    if not isinstance(input_digest, str) or re.fullmatch(r"[0-9a-f]{64}", input_digest) is None:
        raise ValidationError("XilinxRouteDB source.rwroute_input_sha256 is invalid")
    used_pips: Dict[Tuple[str, str, str], str] = {}
    checked_nets = 0
    checked_sinks = 0
    for index, net in enumerate(value.get("nets", [])):
        context = f"route.nets[{index}]"
        if not isinstance(net, dict) or not isinstance(net.get("net"), str):
            raise ValidationError(f"{context}: invalid net")
        if net.get("has_gap"):
            raise ValidationError(f"{context}: RWRoute reports gap routing")
        pins = net.get("pins")
        pips = net.get("pips")
        if not isinstance(pins, list) or not isinstance(pips, list):
            raise ValidationError(f"{context}: pins/PIPs are invalid")
        sources = [pin for pin in pins if pin.get("is_output")]
        sinks = [pin for pin in pins if not pin.get("is_output")]
        if len(sources) != 1 or not sinks:
            raise ValidationError(f"{context}: routed net lacks one source and sinks")
        if any(pin.get("node") is None for pin in pins):
            raise ValidationError(f"{context}: site pin lacks a connected route node")
        graph: Dict[str, Set[str]] = defaultdict(set)
        for pip_index, pip in enumerate(pips):
            if not isinstance(pip, dict):
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid PIP")
            start, end = pip.get("start_node"), pip.get("end_node")
            if not isinstance(start, str) or not isinstance(end, str) or start == end:
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid directed adjacency")
            tile = pip.get("tile")
            start_wire = pip.get("start_wire")
            end_wire = pip.get("end_wire")
            if any(
                not isinstance(item, str) or not item
                for item in (tile, start_wire, end_wire)
            ):
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid PIP identity")
            wires = sorted([start_wire, end_wire])
            key = (tile, wires[0], wires[1])
            previous = used_pips.get(key)
            if previous is not None and previous != net["net"]:
                raise ValidationError(f"routing conflict on PIP {key}")
            used_pips[key] = net["net"]
            graph[start].add(end)
        source_node = sources[0]["node"]
        reachable = {source_node}
        work = deque([source_node])
        while work:
            current = work.popleft()
            for following in graph.get(current, set()):
                if following not in reachable:
                    reachable.add(following)
                    work.append(following)
        missing = sorted(pin["node"] for pin in sinks if pin["node"] not in reachable)
        if missing:
            raise ValidationError(
                f"{context}: {len(missing)} sinks are not source-connected"
            )
        checked_nets += 1
        checked_sinks += len(sinks)
    value["status"] = "pass"
    write_json(path, value, compact=True)
    return {
        "status": "pass", "schema": "emuflow.xilinx-route-validation/v1",
        "nets": checked_nets, "sinks": checked_sinks,
        "pips": len(used_pips), "route_sha256": _sha256(path),
        "excluded_nets": len(value.get("excluded_nets", [])),
    }
