"""Bind BoardDB resource limits to a circuit-independent fixed VTR grid.

The generated XML is the canonical owner of geometry and capacity. BoardDB
retains its compact identity/capacity contract, not another copy of the grid.
Scalar capacities are necessary bounds; VPR still proves mode-aware packing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .errors import ValidationError
from .io import read_json, write_json
from .native_tools import resolve_native_executable


SCHEMA = "emuflow.fixed-vtr-device/v1"


def vpr_device_arguments(architecture: Path) -> list:
    layouts = ET.parse(architecture).getroot().find("layout")
    if layouts is not None and len(layouts) == 1 and layouts[0].tag == "fixed_layout":
        return ["--device", layouts[0].attrib["name"]]
    return []  # Low-level standalone diagnostics only; full flow rejects auto.


def _integer(value, context):
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{context}: expected integer") from None
    if isinstance(value, bool) or str(result) != str(value) or result < 0:
        raise ValidationError(f"{context}: expected nonnegative integer")
    return result


def _pb_resources(pb):
    models = {p.get("blif_model") for p in pb.iter("pb_type") if p.get("blif_model")}
    # A dedicated physical hard-block site is one conservative macro slot,
    # NOT the sum of alternative modes or memory bit-slice atoms.
    if models and models <= {".subckt single_port_ram", ".subckt dual_port_ram"}:
        return Counter(bram=1)
    if models == {".subckt multiply"}:
        return Counter(dsp=1)
    if models and models <= {".input", ".output"}:
        return Counter(io=1)

    def walk(node):
        model = node.get("blif_model")
        if model:
            if model == ".names":
                pins = sum(_integer(p.get("num_pins"), "LUT input") for p in node.findall("input"))
                if pins > 6 or pins <= 0:
                    raise ValidationError("fixed-device supports LUTs up to six inputs")
                # Phase 3 counts generic LUTs up to LUT6. Smaller fracturing
                # modes must not increase the advertised LUT6 capacity.
                return Counter(lut=1 if pins == 6 else 0)
            if model == ".latch":
                return Counter(ff=1)
            if model == ".subckt adder":
                return Counter(carry=1)
            raise ValidationError(f"unsupported mixed physical primitive {model!r}")
        modes = node.findall("mode")
        if modes and node.findall("pb_type"):
            raise ValidationError("mixed direct pb children and modes")
        alternatives = []
        for parent in modes or [node]:
            counts = Counter()
            for child in parent.findall("pb_type"):
                scale = _integer(child.get("num_pb", "1"), "num_pb")
                counts.update({k: v * scale for k, v in walk(child).items()})
            alternatives.append(counts)
        return Counter({k: max(c[k] for c in alternatives) for k in {k for c in alternatives for k in c}})

    return walk(pb)


def device_contract(architecture: Path) -> dict:
    data = architecture.read_bytes()
    root = ET.fromstring(data)
    layouts = list(root.find("layout") or [])
    if len(layouts) != 1 or layouts[0].tag != "fixed_layout":
        raise ValidationError("physical capacity requires exactly one fixed VTR layout, not auto-size")
    layout = layouts[0]
    width = _integer(layout.get("width"), "width")
    height = _integer(layout.get("height"), "height")
    if min(width, height) <= 2 or not layout.get("name"):
        raise ValidationError("fixed layout requires name and dimensions greater than two")
    tiles = {t.get("name"): t for t in root.findall("tiles/tile")}
    pbs = {p.get("name"): p for p in root.findall("complexblocklist/pb_type")}
    capacities = {}
    for name, tile in tiles.items():
        total = Counter()
        for sub in tile.findall("sub_tile"):
            choices = []
            for site in sub.findall("equivalent_sites/site"):
                if site.get("pb_type") not in pbs:
                    raise ValidationError("unknown equivalent physical block")
                choices.append(_pb_resources(pbs[site.get("pb_type")]))
            if not choices:
                raise ValidationError("physical sub-tile has no equivalent sites")
            scale = _integer(sub.get("capacity", "1"), "sub-tile capacity")
            total.update({k: max(c[k] for c in choices) * scale for k in {k for c in choices for k in c}})
        capacities[name] = total
    occupied = set()
    totals, tile_counts = Counter(), Counter()
    for rule in layout:
        if rule.tag == "fill" and rule.get("type") == "EMPTY":
            continue
        if rule.tag != "single" or set(rule.attrib) - {"type", "priority", "x", "y"}:
            raise ValidationError("fixed capacity requires materialized single-tile locations")
        tile_name = rule.get("type")
        if tile_name not in tiles:
            raise ValidationError("fixed layout contains unknown tile")
        tile = tiles[tile_name]
        x, y = (_integer(rule.get(k), k) for k in ("x", "y"))
        tw, th = (_integer(tile.get(k, "1"), k) for k in ("width", "height"))
        if not tw or not th or x + tw > width or y + th > height:
            raise ValidationError("fixed tile footprint exceeds device grid")
        footprint = {(x + dx, y + dy) for dx in range(tw) for dy in range(th)}
        if occupied & footprint:
            raise ValidationError("overlapping fixed physical tiles")
        occupied.update(footprint)
        totals.update(capacities[tile_name])
        tile_counts[tile_name] += 1
    if not totals.get("lut"):
        raise ValidationError("fixed physical grid has no LUT6 capacity")
    return {"schema": SCHEMA, "architecture_sha256": hashlib.sha256(data).hexdigest(),
            "layout": layout.get("name"), "width": width, "height": height,
            "capacity": dict(sorted(totals.items())), "tiles": dict(sorted(tile_counts.items())),
            "qualification": "fixed-grid-scalar-upper-bounds; VPR-mode-packing-required"}


def validate_platform_device(platform, architecture: Path) -> dict:
    contract = device_contract(architecture)
    if platform.physical_device != contract:
        raise ValidationError("BoardDB physical device identity/capacity does not match fixed XML; materialize a bound platform first")
    if any(f.capacity != contract["capacity"] for f in platform.fpgas):
        raise ValidationError("BoardDB FPGA capacity differs from fixed physical grid")
    return contract


def materialize_device(architecture: Path, board: Path, output_xml: Path,
                       output_board: Path, width: int, height: int, *, executable=None):
    from .platform import Platform
    from .vtr_architecture import _parse_extract, _layout_grid

    if output_xml.resolve() == output_board.resolve():
        raise ValidationError("fixed XML and BoardDB require distinct output paths")
    root = ET.parse(architecture).getroot()
    layout = root.find("layout")
    if layout is None or len(layout) != 1:
        raise ValidationError("select an architecture with one layout before materialization")
    # The current importer intentionally supports a subset of VTR layout
    # syntax. Reject other expressions/rules rather than dropping them.
    allowed = {"fill", "perimeter", "corners", "col", "row", "single"}
    for rule in layout[0]:
        if rule.tag not in allowed or set(rule.attrib) - {"type", "priority", "startx", "starty", "repeatx", "repeaty", "x", "y"}:
            raise ValidationError("unsupported layout rule; cannot derive fixed capacity")
    tool = resolve_native_executable("emuflow_vtr_arch_importer", executable)
    result = subprocess.run([tool, str(architecture)], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValidationError(f"architecture extraction failed: {result.stdout}{result.stderr}")
    extract = _parse_extract(result.stdout)
    grid = _layout_grid(extract, width=width, height=height)
    layout.clear()
    fixed = ET.SubElement(layout, "fixed_layout", name=f"emuflow_{width}x{height}", width=str(width), height=str(height))
    ET.SubElement(fixed, "fill", type="EMPTY", priority="1")
    for (x, y), entry in sorted(grid.items()):
        if entry["root"] != (x, y) or entry["type"] == "EMPTY":
            continue
        tile = extract["tiles"][entry["type"]]
        if any(grid.get((x + dx, y + dy), {}).get("root") != (x, y)
               for dx in range(tile["width"]) for dy in range(tile["height"])):
            raise ValidationError("partially overwritten physical tile footprint")
        ET.SubElement(fixed, "single", type=entry["type"], priority="100", x=str(x), y=str(y))
    for path in (output_xml, output_board):
        if path.exists() or path.is_symlink() or path.resolve() in {architecture.resolve(), board.resolve()}:
            raise ValidationError("materialization outputs must be new, separate files")
    output_xml.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_xml, encoding="utf-8", xml_declaration=True)
    contract = device_contract(output_xml)
    value = copy.deepcopy(read_json(board))
    for fpga in value["fpgas"]:
        fpga["capacity"] = contract["capacity"]
        fpga["part"] = f"vtr:{contract['layout']}"
    value["physical_device"] = contract
    platform = Platform.from_dict(value)
    validate_platform_device(platform, output_xml)
    write_json(output_board, platform.to_dict())
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("architecture", "board", "output-xml", "output-board"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--importer")
    args = parser.parse_args()
    materialize_device(args.architecture, args.board, args.output_xml, args.output_board,
                       args.width, args.height, executable=args.importer)


if __name__ == "__main__":
    main()
