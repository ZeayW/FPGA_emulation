"""Compact RWRoute adapter and independent route-certificate checker."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_PLACEMENT_SCHEMA


XILINX_ROUTE_DB_SCHEMA = "emuflow.xilinx-route-db/v1"
RAPIDWRIGHT_TIMING_DATA_REVISION = (
    "127f55cd704c277372697e699f1559e1cdc91f34"
)
RAPIDWRIGHT_TIMING_DATA_SHA256 = {
    "intersite_delay_terms.txt": (
        "3b122837c4a1b5f3c212fc6353bee3a0854a204f2f69e2bc1cac4a2a1f9d7333"
    ),
    "intrasite_delay_terms.txt": (
        "ff08ce9041da649f2bf886d900f033dd23de8ad54eb4db55d37ce032dc0ec0a0"
    ),
}
RAPIDWRIGHT_DEVICE_DATA_MD5 = {
    "data/parts.db": "58dd6f20c37798322b6904a8a786a3de",
    "data/devices/virtexuplus/xcvu19p_db.dat": (
        "5ad01490fe442f360aa67d7dfe0fa1c3"
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_rapidwright_device_data(root: Path) -> Dict[str, str]:
    observed: Dict[str, str] = {}
    for relative, expected_md5 in RAPIDWRIGHT_DEVICE_DATA_MD5.items():
        path = root / relative
        actual_md5 = _md5(path) if path.is_file() else "missing"
        if actual_md5 != expected_md5:
            raise ValidationError(
                "RapidWright device data is missing or does not match the "
                f"pinned XCVU19P provider: {path}"
            )
        observed[relative] = actual_md5
    return observed


def _select_module(mapped: Mapping[str, Any], top: str) -> Mapping[str, Any]:
    modules = mapped.get("modules")
    module = modules.get(top) if isinstance(modules, dict) else None
    if not isinstance(module, dict) or not isinstance(module.get("cells"), dict):
        raise ValidationError(f"mapped JSON has no valid top module {top!r}")
    return module


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def _primitive_parameter(parameters: Mapping[str, Any], name: str) -> Optional[str]:
    """Return a compact RapidWright property value for a routed hard primitive.

    Yosys JSON writes integer parameters as binary strings.  RWRoute needs the
    decoded width/register value before the primitive is placed because the
    physical pin expansion of a RAMB36 write-enable depends on TDP versus SDP
    mode.  Large INIT payloads are deliberately excluded from this hot path.
    """
    value = parameters.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise ValidationError(f"primitive parameter {name!r} has invalid type")
    if value and set(value) <= {"0", "1"}:
        return str(int(value, 2))
    if any(character in value for character in "\t\r\n"):
        raise ValidationError(f"primitive parameter {name!r} is not TSV-safe")
    return value


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
    transformed_dsp48e2 = 0
    for name in sorted(physical):
        site, bel = physical[name]
        cell_type = cells[name]["type"]
        if cell_type != "LUT6_2":
            route_cells[name] = (name, cell_type, site, bel)
            if cell_type == "DSP48E2":
                transformed_dsp48e2 += 1
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
        pin_bindings[(name, "I1")] = (o6_key, "I1")
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
        if cell_type in {"RAMB18E2", "RAMB36E2"}:
            parameters = cells[route_name].get("parameters", {})
            if not isinstance(parameters, dict):
                raise ValidationError(
                    f"hard primitive {display_name!r} parameters are invalid"
                )
            for parameter in (
                "READ_WIDTH_A", "READ_WIDTH_B", "WRITE_WIDTH_A",
                "WRITE_WIDTH_B", "DOA_REG", "DOB_REG", "WRITE_MODE_A",
                "WRITE_MODE_B", "RAM_MODE",
            ):
                value = _primitive_parameter(parameters, parameter)
                if value is not None:
                    lines.append(
                        f"PARAM\t{safe[route_name]}\t{parameter}\t{value}"
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
        boundary_clock = any(
            pin in {"C", "CLK"} for _cell, pin, _role in sinks
        )
        if len(drivers) != 1 or not sinks:
            if not drivers and sinks and boundary_clock:
                lines.append(
                    f"EXCLUDED\t{net_name}\tboundary_clock"
                    "\tideal-boundary-clock"
                )
            else:
                reason = (
                    "boundary_or_driverless"
                    if not drivers else "multiple_driver_or_sinkless"
                )
                lines.append(f"EXCLUDED\t{net_name}\t{reason}")
            excluded += 1
            continue
        sites = {physical[name][0] for name, _pin, _role in [drivers[0], *sinks]}
        if len(sites) == 1:
            lines.append(f"EXCLUDED\t{net_name}\tintra_site")
            excluded += 1
            continue
        kind = "clock" if boundary_clock else "signal"
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
        "transformed_dsp48e2_cells": transformed_dsp48e2,
        "physical_cells": len(safe) + 7 * transformed_dsp48e2,
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
    device_data_root: Path,
    timing_data_dir: Path,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    classes_dir.mkdir(parents=True, exist_ok=True)
    runtime_home = classes_dir.parent / "rapidwright-runtime-home"
    runtime_home.mkdir(parents=True, exist_ok=True)
    rapidwright_path = runtime_home / "RapidWright"
    rapidwright_path.mkdir(parents=True, exist_ok=True)
    device_data_md5 = _validate_rapidwright_device_data(device_data_root)
    runtime_data = rapidwright_path / "data"
    provider_data = (device_data_root / "data").resolve()
    if runtime_data.is_symlink():
        if runtime_data.resolve() != provider_data:
            runtime_data.unlink()
    elif runtime_data.exists():
        shutil.rmtree(runtime_data)
    if not runtime_data.exists():
        runtime_data.symlink_to(provider_data, target_is_directory=True)
    runtime_timing_dir = rapidwright_path / "timing" / "ultrascaleplus"
    runtime_timing_dir.mkdir(parents=True, exist_ok=True)
    for name, expected_sha256 in RAPIDWRIGHT_TIMING_DATA_SHA256.items():
        source = timing_data_dir / name
        if not source.is_file() or _sha256(source) != expected_sha256:
            raise ValidationError(
                "RapidWright timing data is missing or does not match pinned "
                f"revision {RAPIDWRIGHT_TIMING_DATA_REVISION}: {source}"
            )
        destination = runtime_timing_dir / name
        if not destination.is_file() or _sha256(destination) != expected_sha256:
            shutil.copyfile(source, destination)
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
    temporary_log = log_path is None
    if temporary_log:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="emuflow-rwroute-", suffix=".log"
        )
        os.close(descriptor)
        route_log_path = Path(temporary_name)
    else:
        route_log_path = log_path
        assert route_log_path is not None
        route_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with route_log_path.open("w", encoding="utf-8") as route_log:
            completed = subprocess.run(
                command, stdout=route_log, stderr=subprocess.STDOUT,
                text=True, check=False,
                env={**os.environ, "RAPIDWRIGHT_PATH": str(rapidwright_path)},
            )
        with route_log_path.open("rb") as route_log:
            route_log.seek(0, os.SEEK_END)
            route_log.seek(max(0, route_log.tell() - 8000), os.SEEK_SET)
            failure_tail = route_log.read().decode("utf-8", errors="replace")
    finally:
        if temporary_log:
            route_log_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise ValidationError(
            f"RWRoute failed with exit code {completed.returncode}:\n"
            + "\n".join(failure_tail.splitlines()[-80:])
        )
    value = read_json(output_path)
    timing = value.get("timing")
    if not isinstance(timing, dict):
        raise ValidationError("RWRoute output lacks its timing qualification")
    timing["source_revision"] = RAPIDWRIGHT_TIMING_DATA_REVISION
    timing["source_data_sha256"] = dict(RAPIDWRIGHT_TIMING_DATA_SHA256)
    timing["device_data_md5"] = device_data_md5
    write_json(output_path, value, compact=True)
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
    if value.get("provider") != "rapidwright-rwroute-2026.1.0":
        raise ValidationError("XilinxRouteDB provider identity is invalid")
    route_cells = value.get("cells")
    materialization = value.get("materialization")
    if (
        isinstance(route_cells, bool)
        or not isinstance(route_cells, int)
        or route_cells < 0
        or not isinstance(materialization, dict)
        or materialization.get("route_cells") != route_cells
    ):
        raise ValidationError("XilinxRouteDB materialized route-cell count is invalid")
    physical_cells = materialization.get("physical_cells")
    transformed_dsp48e2 = materialization.get("transformed_dsp48e2_cells")
    router = materialization.get("router")
    if (
        isinstance(physical_cells, bool)
        or not isinstance(physical_cells, int)
        or isinstance(transformed_dsp48e2, bool)
        or not isinstance(transformed_dsp48e2, int)
        or transformed_dsp48e2 < 0
        or transformed_dsp48e2 > route_cells
        or physical_cells != route_cells + 7 * transformed_dsp48e2
        or router
        != (
            "CUFR-HUS-non-timing-driven-uturn-enabled-"
            "serial-unroutable-recovery"
        )
    ):
        raise ValidationError("XilinxRouteDB transformed-cell accounting is invalid")
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
    if mapped_path is not None and packed_path is not None:
        mapped = read_json(mapped_path)
        packed = read_json(packed_path)
        top = packed.get("top")
        if not isinstance(top, str):
            raise ValidationError("PackedSiteNetlist top is invalid")
        mapped_cells = _select_module(mapped, top)["cells"]
        expected_lut6_2 = sum(
            cell.get("type") == "LUT6_2" for cell in mapped_cells.values()
        )
        expected_dsp48e2 = sum(
            cell.get("type") == "DSP48E2" for cell in mapped_cells.values()
        )
        expected_route_cells = len(mapped_cells) + expected_lut6_2
        if route_cells != expected_route_cells:
            raise ValidationError("XilinxRouteDB route-cell accounting disagrees")
        if transformed_dsp48e2 != expected_dsp48e2:
            raise ValidationError("XilinxRouteDB DSP48E2 transform count disagrees")
    input_digest = source.get("rwroute_input_sha256")
    if not isinstance(input_digest, str) or re.fullmatch(r"[0-9a-f]{64}", input_digest) is None:
        raise ValidationError("XilinxRouteDB source.rwroute_input_sha256 is invalid")
    route_nets = value.get("nets")
    excluded_nets = value.get("excluded_nets")
    summary = value.get("summary")
    if (
        not isinstance(route_nets, list)
        or not isinstance(excluded_nets, list)
        or not isinstance(summary, dict)
    ):
        raise ValidationError("XilinxRouteDB net collections are invalid")
    used_pips: Dict[Tuple[str, str, str], str] = {}
    seen_nets: Set[str] = set()
    checked_nets = 0
    checked_sinks = 0
    static_nets = 0
    static_sinks = 0
    clock_nets = 0
    nets_with_pips = 0
    route_delays_ps: List[float] = []
    for index, net in enumerate(route_nets):
        context = f"route.nets[{index}]"
        if not isinstance(net, dict) or not isinstance(net.get("net"), str):
            raise ValidationError(f"{context}: invalid net")
        net_name = net["net"]
        if not net_name or net_name in seen_nets:
            raise ValidationError(f"{context}: duplicate or empty net identity")
        seen_nets.add(net_name)
        if net.get("has_gap") is not False:
            raise ValidationError(f"{context}: RWRoute reports gap routing")
        pins = net.get("pins")
        alternate_sources = net.get("alternate_sources", [])
        pips = net.get("pips")
        if (
            not isinstance(pins, list)
            or not isinstance(alternate_sources, list)
            or not isinstance(pips, list)
        ):
            raise ValidationError(f"{context}: pins/PIPs are invalid")
        if not pins:
            raise ValidationError(f"{context}: routed net has no site pins")
        for pin_index, pin in enumerate(pins):
            if (
                not isinstance(pin, dict)
                or not isinstance(pin.get("site"), str)
                or not pin["site"]
                or not isinstance(pin.get("pin"), str)
                or not pin["pin"]
                or not isinstance(pin.get("is_output"), bool)
                or not isinstance(pin.get("node"), str)
                or not pin["node"]
            ):
                raise ValidationError(f"{context}.pins[{pin_index}]: invalid site pin")
        for pin_index, pin in enumerate(alternate_sources):
            if (
                not isinstance(pin, dict)
                or not isinstance(pin.get("site"), str)
                or not pin["site"]
                or not isinstance(pin.get("pin"), str)
                or not pin["pin"]
                or pin.get("is_output") is not True
                or not isinstance(pin.get("node"), str)
                or not pin["node"]
                or "route_delay_ps" in pin
            ):
                raise ValidationError(
                    f"{context}.alternate_sources[{pin_index}]: invalid physical source"
                )
        sources = [pin for pin in pins if pin["is_output"]]
        sinks = [pin for pin in pins if not pin["is_output"]]
        kind = net.get("kind")
        qualification = net.get("qualification")
        static = kind in {"static_gnd", "static_vcc"}
        if kind == "signal":
            if qualification != "ordinary-fabric-signal":
                raise ValidationError(f"{context}: signal qualification is invalid")
        elif kind == "clock":
            if qualification != "fabric-routed-clock":
                raise ValidationError(f"{context}: clock qualification is invalid")
            clock_nets += 1
        elif static:
            if qualification != "device-tied-static":
                raise ValidationError(f"{context}: static qualification is invalid")
            static_nets += 1
            static_sinks += len(sinks)
        else:
            raise ValidationError(f"{context}: unsupported routed net kind")
        if static:
            roots = net.get("roots")
            if (
                sources
                or alternate_sources
                or not sinks
                or not isinstance(roots, list)
                or not roots
                or any(not isinstance(root, str) or not root for root in roots)
                or len(set(roots)) != len(roots)
            ):
                raise ValidationError(f"{context}: static route roots/sinks are invalid")
            source_nodes = set(roots)
        else:
            if len(sources) != 1 or not sinks or "roots" in net:
                raise ValidationError(f"{context}: routed net lacks one source and sinks")
            source_nodes = {
                sources[0]["node"],
                *(pin["node"] for pin in alternate_sources),
            }
            if (
                len(source_nodes) != 1 + len(alternate_sources)
                or source_nodes.intersection(pin["node"] for pin in sinks)
            ):
                raise ValidationError(
                    f"{context}: duplicate or conflicting physical route source"
                )
        if net.get("source_present") is not bool(sources):
            raise ValidationError(f"{context}: source-presence summary disagrees")
        if net.get("sink_count") != len(sinks):
            raise ValidationError(f"{context}: sink-count summary disagrees")
        for pin_index, pin in enumerate(pins):
            delay = pin.get("route_delay_ps")
            if static:
                if delay is not None:
                    raise ValidationError(
                        f"{context}.pins[{pin_index}]: static pin has a route delay"
                    )
                continue
            if pin["is_output"]:
                if delay is not None:
                    raise ValidationError(
                        f"{context}.pins[{pin_index}]: source has a route delay"
                    )
                continue
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(float(delay))
                or float(delay) < 0.0
            ):
                raise ValidationError(
                    f"{context}.pins[{pin_index}]: route delay is invalid"
                )
            route_delays_ps.append(float(delay))
        graph: Dict[str, Set[str]] = defaultdict(set)
        local_pips: Set[Tuple[str, str, str]] = set()
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
            if key in local_pips:
                raise ValidationError(f"{context}: duplicate PIP {key}")
            local_pips.add(key)
            previous = used_pips.get(key)
            if previous is not None and previous != net_name:
                raise ValidationError(f"routing conflict on PIP {key}")
            used_pips[key] = net_name
            graph[start].add(end)
        if pips:
            nets_with_pips += 1
        reachable = set(source_nodes)
        work = deque(sorted(source_nodes))
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
    excluded_names: Set[str] = set()
    boundary_clock_nets = 0
    allowed_exclusions = {
        "boundary_or_driverless", "multiple_driver_or_sinkless", "intra_site"
    }
    for index, excluded in enumerate(excluded_nets):
        context = f"route.excluded_nets[{index}]"
        if not isinstance(excluded, dict):
            raise ValidationError(f"{context}: invalid exclusion record")
        net_name, reason = excluded.get("net"), excluded.get("reason")
        if reason == "boundary_clock":
            if (
                set(excluded) != {"net", "reason", "qualification"}
                or excluded.get("qualification") != "ideal-boundary-clock"
            ):
                raise ValidationError(
                    f"{context}: boundary clock qualification is invalid"
                )
            boundary_clock_nets += 1
        elif set(excluded) != {"net", "reason"} or reason not in allowed_exclusions:
            raise ValidationError(f"{context}: invalid exclusion record")
        if (
            not isinstance(net_name, str)
            or not net_name
            or net_name in seen_nets
            or net_name in excluded_names
        ):
            raise ValidationError(f"{context}: invalid or conflicting exclusion")
        excluded_names.add(net_name)
    expected_summary = {
        "candidate_nets": checked_nets - static_nets,
        "certificate_nets": checked_nets,
        "static_nets": static_nets,
        "static_sinks": static_sinks,
        "nets_with_pips": nets_with_pips,
        "pips": len(used_pips),
        "excluded_nets": len(excluded_nets),
        "boundary_clock_nets": boundary_clock_nets,
    }
    if summary != expected_summary:
        raise ValidationError("XilinxRouteDB summary disagrees with route certificate")
    timing = value.get("timing")
    expected_timing = {
        "provider": "rapidwright-lightweight",
        "family": "UltraScalePlus",
        "units": "ps",
        "setup_route_delays": "available",
        "hold_analysis": "unavailable",
        "hard_block_clock_timing": "unqualified",
        "source_revision": RAPIDWRIGHT_TIMING_DATA_REVISION,
        "source_data_sha256": RAPIDWRIGHT_TIMING_DATA_SHA256,
        "device_data_md5": RAPIDWRIGHT_DEVICE_DATA_MD5,
    }
    if not isinstance(timing, dict):
        raise ValidationError("XilinxRouteDB timing qualification is missing")
    for key, expected in expected_timing.items():
        if timing.get(key) != expected:
            raise ValidationError(f"XilinxRouteDB timing.{key} is invalid")
    if timing.get("routed_endpoints") != len(route_delays_ps):
        raise ValidationError("XilinxRouteDB timed endpoint count disagrees")
    logic_coefficients = timing.get("logic_coefficients_ps")
    expected_logic_coefficients = {
        "ff_clock_to_q", "carry_co", "lut_a1", "lut_a2", "lut_a3",
        "lut_a4", "lut_a5", "lut_a6",
    }
    if (
        not isinstance(logic_coefficients, dict)
        or set(logic_coefficients) != expected_logic_coefficients
    ):
        raise ValidationError(
            "XilinxRouteDB logic timing coefficients are invalid"
        )
    for name, delay in logic_coefficients.items():
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError(
                f"XilinxRouteDB logic timing coefficient {name!r} is invalid"
            )
    maximum_route_delay_ps = max(route_delays_ps, default=0.0)
    reported_maximum = timing.get("maximum_route_delay_ps")
    if (
        isinstance(reported_maximum, bool)
        or not isinstance(reported_maximum, (int, float))
        or not math.isclose(
            float(reported_maximum), maximum_route_delay_ps,
            rel_tol=1.0e-6, abs_tol=1.0e-3,
        )
    ):
        raise ValidationError("XilinxRouteDB maximum route delay disagrees")
    value["status"] = "pass"
    write_json(path, value, compact=True)
    return {
        "status": "pass", "schema": "emuflow.xilinx-route-validation/v1",
        "nets": checked_nets, "sinks": checked_sinks,
        "pips": len(used_pips), "route_sha256": _sha256(path),
        "excluded_nets": len(value.get("excluded_nets", [])),
        "boundary_clock_nets": boundary_clock_nets,
        "timed_endpoints": len(route_delays_ps),
        "maximum_route_delay_ps": maximum_route_delay_ps,
        "timing_provider": timing["provider"],
        "hold_analysis": timing["hold_analysis"],
        "route_cells": route_cells,
        "physical_cells": physical_cells,
        "transformed_dsp48e2_cells": transformed_dsp48e2,
        "static_nets": static_nets,
        "static_sinks": static_sinks,
        "clock_nets": clock_nets,
    }
