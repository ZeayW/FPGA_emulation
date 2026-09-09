"""VTR architecture XML adapter for the open academic default backend."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .architecture import ARCHDB_SCHEMA, ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .native_tools import resolve_native_executable


ARCHITECTURE_TIMING_DB_SCHEMA = "emuflow.architecture-timing-db/v1"
VTR_SOURCE_FORMAT = "vtr-architecture-xml/v1"
_EXTRACT_HEADER = "EMUFLOW_VTR_ARCHITECTURE_EXTRACT_V1"
_VPR_ARRAY_SIZE = re.compile(
    r"^Array size:\s+(\d+)\s+x\s+(\d+)\s+logic blocks\s*$"
)


def _runtime_data_path(relative: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = (
        root / relative,
        root / "share" / "emuflow" / relative,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


DEFAULT_VTR_ARCHITECTURE_SOURCE = _runtime_data_path(
    Path("resources/architectures/vtr/flagship-k6-n10-40nm.json")
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_vpr_placement_dimensions(path: Path) -> tuple[int, int]:
    """Read the exact auto-layout dimensions recorded by a VPR placement."""

    if not path.is_file():
        raise ValidationError(f"VPR placement does not exist: {path}")
    for line in path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        match = _VPR_ARRAY_SIZE.match(line.strip())
        if match:
            width, height = (int(value) for value in match.groups())
            if width > 0 and height > 0:
                return width, height
            break
    raise ValidationError(
        f"VPR placement has no valid Array size header: {path}"
    )


def fetch_pinned_vtr_architecture(
    output_path: Path,
    manifest_path: Path = DEFAULT_VTR_ARCHITECTURE_SOURCE,
) -> Dict[str, Any]:
    manifest = read_json(manifest_path)
    if (
        manifest.get("schema")
        != "emuflow.pinned-architecture-source/v1"
    ):
        raise ValidationError("VTR architecture source manifest is invalid")
    url = manifest.get("raw_url")
    expected = manifest.get("sha256")
    if not isinstance(url, str) or not url:
        raise ValidationError("VTR architecture source URL is invalid")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise ValidationError("VTR architecture source SHA-256 is invalid")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read()
    except OSError as error:
        raise ValidationError(
            f"failed to fetch pinned VTR architecture: {error}"
        ) from error
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ValidationError(
            "pinned VTR architecture SHA-256 mismatch: "
            f"expected {expected}, got {actual}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".part")
    temporary.write_bytes(content)
    temporary.replace(output_path)
    return {
        "status": "pass",
        "name": manifest.get("name"),
        "output": str(output_path),
        "sha256": actual,
        "source": url,
        "qualification": manifest.get("qualification"),
    }


def _decode(value: str, context: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"{context}: invalid UTF-8 hex") from error


def _integer(value: str, context: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValidationError(f"{context}: invalid integer") from error
    return result


def _number(value: str, context: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise ValidationError(f"{context}: invalid number") from error
    if not math.isfinite(result) or result < 0.0:
        raise ValidationError(f"{context}: expected a non-negative number")
    return result


def _parse_extract(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != _EXTRACT_HEADER:
        raise ValidationError("VTR architecture importer header is invalid")
    value: Dict[str, Any] = {
        "layouts": [],
        "rules": [],
        "tiles": {},
        "resources": defaultdict(dict),
        "primitives": {},
        "primitive_arcs": [],
        "block_interconnect_arcs": [],
        "switches": [],
        "segments": [],
        "directs": [],
    }
    for line_number, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        context = f"native extract line {line_number}"
        record = fields[0]
        if record == "LAYOUT" and len(fields) == 6:
            value["layouts"].append(
                {
                    "kind": _decode(fields[1], context),
                    "name": _decode(fields[2], context),
                    "width": _integer(fields[3], context),
                    "height": _integer(fields[4], context),
                    "aspect_ratio": _number(fields[5], context),
                }
            )
        elif record == "RULE" and len(fields) == 9:
            value["rules"].append(
                {
                    "layout": _decode(fields[1], context),
                    "kind": _decode(fields[2], context),
                    "type": _decode(fields[3], context),
                    "priority": _integer(fields[4], context),
                    "startx": _integer(fields[5], context),
                    "starty": _integer(fields[6], context),
                    "repeatx": _integer(fields[7], context),
                    "repeaty": _integer(fields[8], context),
                }
            )
        elif record == "TILE" and len(fields) == 4:
            name = _decode(fields[1], context)
            if name in value["tiles"]:
                raise ValidationError(f"{context}: duplicate tile {name!r}")
            value["tiles"][name] = {
                "name": name,
                "width": _integer(fields[2], context),
                "height": _integer(fields[3], context),
                "sub_tiles": [],
            }
        elif record == "SUBTILE" and len(fields) == 5:
            tile_name = _decode(fields[1], context)
            if tile_name not in value["tiles"]:
                raise ValidationError(f"{context}: unknown tile {tile_name!r}")
            value["tiles"][tile_name]["sub_tiles"].append(
                {
                    "name": _decode(fields[2], context),
                    "capacity": _integer(fields[3], context),
                    "pb_type": _decode(fields[4], context),
                }
            )
        elif record == "RESOURCE" and len(fields) == 4:
            pb_type = _decode(fields[1], context)
            resource = _decode(fields[2], context)
            count = _integer(fields[3], context)
            if count <= 0 or resource in value["resources"][pb_type]:
                raise ValidationError(f"{context}: invalid resource record")
            value["resources"][pb_type][resource] = count
        elif record == "PRIMITIVE" and len(fields) == 5:
            path = _decode(fields[1], context)
            if path in value["primitives"]:
                raise ValidationError(f"{context}: duplicate primitive path")
            value["primitives"][path] = {
                "path": path,
                "cell": _decode(fields[2], context),
                "model": _decode(fields[3], context),
                "class": _decode(fields[4], context),
                "ports": [],
            }
        elif record == "PORT" and len(fields) == 5:
            path = _decode(fields[1], context)
            if path not in value["primitives"]:
                raise ValidationError(f"{context}: unknown primitive path")
            value["primitives"][path]["ports"].append(
                {
                    "direction": _decode(fields[2], context),
                    "name": _decode(fields[3], context),
                    "width": _integer(fields[4], context),
                }
            )
        elif record == "ARC" and len(fields) == 11:
            value["primitive_arcs"].append(
                {
                    "scope": _decode(fields[1], context),
                    "kind": _decode(fields[2], context),
                    "type": _decode(fields[3], context),
                    "from": _decode(fields[4], context),
                    "to": _decode(fields[5], context),
                    "port": _decode(fields[6], context),
                    "clock": _decode(fields[7], context),
                    "min_seconds": _number(fields[8], context),
                    "max_seconds": _number(fields[9], context),
                    "matrix": _decode(fields[10], context).strip(),
                }
            )
        elif record == "BLOCK_ARC" and len(fields) == 9:
            value["block_interconnect_arcs"].append(
                {
                    "scope": _decode(fields[1], context),
                    "kind": _decode(fields[2], context),
                    "type": _decode(fields[3], context),
                    "from": _decode(fields[4], context),
                    "to": _decode(fields[5], context),
                    "min_seconds": _number(fields[6], context),
                    "max_seconds": _number(fields[7], context),
                    "matrix": _decode(fields[8], context).strip(),
                }
            )
        elif record == "SWITCH" and len(fields) == 7:
            value["switches"].append(
                {
                    "name": _decode(fields[1], context),
                    "type": _decode(fields[2], context),
                    "resistance_ohm": _number(fields[3], context),
                    "input_capacitance_f": _number(fields[4], context),
                    "output_capacitance_f": _number(fields[5], context),
                    "intrinsic_delay_seconds": _number(fields[6], context),
                }
            )
        elif record == "SEGMENT" and len(fields) == 8:
            value["segments"].append(
                {
                    "id": _integer(fields[1], context),
                    "length": _integer(fields[2], context),
                    "type": _decode(fields[3], context),
                    "frequency": _number(fields[4], context),
                    "metal_resistance_ohm": _number(fields[5], context),
                    "metal_capacitance_f": _number(fields[6], context),
                    "mux": _decode(fields[7], context),
                }
            )
        elif record == "DIRECT" and len(fields) == 8:
            value["directs"].append(
                {
                    "name": _decode(fields[1], context),
                    "from": _decode(fields[2], context),
                    "to": _decode(fields[3], context),
                    "x_offset": _integer(fields[4], context),
                    "y_offset": _integer(fields[5], context),
                    "z_offset": _integer(fields[6], context),
                    "switch": _decode(fields[7], context),
                }
            )
        else:
            raise ValidationError(f"{context}: malformed {record!r} record")
    value["resources"] = {
        name: dict(sorted(resources.items()))
        for name, resources in sorted(value["resources"].items())
    }
    value["primitives"] = [
        value["primitives"][path] for path in sorted(value["primitives"])
    ]
    return value


def _resource_bels(resources: Mapping[str, int]) -> list[Dict[str, Any]]:
    bels: list[Dict[str, Any]] = []
    lut_counts = {
        int(name[3:]): count
        for name, count in resources.items()
        if name.startswith("LUT") and name[3:].isdigit()
    }
    if lut_counts:
        capacity_by_width = {
            width: max(
                (
                    count
                    for leaf_width, count in lut_counts.items()
                    if leaf_width >= width
                ),
                default=0,
            )
            for width in range(1, max(lut_counts) + 1)
        }
        for index in range(max(capacity_by_width.values())):
            compatible = [
                f"LUT{width}"
                for width, count in sorted(capacity_by_width.items())
                if index < count
            ]
            bels.append(
                {
                    "name": f"LUT_SLOT_{index}",
                    "type": "VTR_LUT",
                    "z": index,
                    "compatible_cells": compatible,
                }
            )

    grouped_prefixes = ("MULT_", "MEM_", "RAM_")
    consumed = {
        name
        for name in resources
        if name.startswith("LUT") and name[3:].isdigit()
    }
    for prefix in grouped_prefixes:
        group = {
            name: count
            for name, count in resources.items()
            if name.startswith(prefix)
        }
        consumed.update(group)
        if not group:
            continue
        for index in range(max(group.values())):
            compatible = [
                name for name, count in sorted(group.items()) if index < count
            ]
            bels.append(
                {
                    "name": f"{prefix.rstrip('_')}_SLOT_{index}",
                    "type": f"VTR_{prefix.rstrip('_')}",
                    "z": index,
                    "compatible_cells": compatible,
                }
            )

    for name, count in sorted(resources.items()):
        if name in consumed:
            continue
        for index in range(count):
            bels.append(
                {
                    "name": f"{name}_SLOT_{index}",
                    "type": f"VTR_{name}",
                    "z": index,
                    "compatible_cells": [name],
                }
            )
    return bels


def _tile_templates(extract: Mapping[str, Any]) -> Dict[str, Any]:
    templates = {}
    for tile_name, tile in sorted(extract["tiles"].items()):
        aggregate: Dict[str, int] = defaultdict(int)
        cluster_capacity: Dict[str, int] = defaultdict(int)
        sub_tiles: Dict[str, Dict[str, Any]] = {}
        for record in tile["sub_tiles"]:
            sub_tile = sub_tiles.setdefault(
                record["name"],
                {"capacity": record["capacity"], "pb_types": []},
            )
            if sub_tile["capacity"] != record["capacity"]:
                raise ValidationError(
                    f"VTR sub_tile {record['name']!r} has inconsistent "
                    "capacity"
                )
            sub_tile["pb_types"].append(record["pb_type"])
        for sub_tile in sub_tiles.values():
            equivalent_capacity: Dict[str, int] = defaultdict(int)
            for pb_type in sub_tile["pb_types"]:
                cluster_capacity[pb_type] += sub_tile["capacity"]
                if pb_type not in extract["resources"]:
                    raise ValidationError(
                        f"VTR tile {tile_name!r} references unknown pb_type "
                        f"{pb_type!r}"
                    )
                for resource, count in extract["resources"][pb_type].items():
                    equivalent_capacity[resource] = max(
                        equivalent_capacity[resource], count
                    )
            for resource, count in equivalent_capacity.items():
                aggregate[resource] += count * sub_tile["capacity"]
        bels = _resource_bels(aggregate)
        if bels:
            templates[tile_name] = {
                "bels": bels,
                "vtr_tile": {
                    "width": tile["width"],
                    "height": tile["height"],
                },
                "vtr_cluster_capacity": dict(
                    sorted(cluster_capacity.items())
                ),
            }
    return templates


def _selected_layout(
    extract: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> tuple[Mapping[str, Any], int, int]:
    if width <= 2 or height <= 2:
        raise ValidationError("VTR virtual device width/height must exceed two")
    layouts = extract["layouts"]
    if not layouts:
        raise ValidationError("VTR architecture contains no supported layout")
    layout = layouts[0]
    if layout["kind"] == "fixed_layout":
        fixed_width = layout["width"]
        fixed_height = layout["height"]
        if fixed_width <= 0 or fixed_height <= 0:
            raise ValidationError("VTR fixed layout has invalid dimensions")
        if (width, height) != (fixed_width, fixed_height):
            raise ValidationError(
                "requested dimensions do not match VTR fixed layout"
            )
    return layout, width, height


def _layout_grid(
    extract: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> Dict[tuple[int, int], Dict[str, Any]]:
    layout, width, height = _selected_layout(
        extract, width=width, height=height
    )
    rules = sorted(
        (
            rule
            for rule in extract["rules"]
            if rule["layout"] == layout["name"]
        ),
        key=lambda rule: (rule["priority"], rule["kind"], rule["type"]),
    )
    grid: Dict[tuple[int, int], Dict[str, Any]] = {}

    def place(
        x: int, y: int, tile_type: str, priority: int
    ) -> None:
        tile = extract["tiles"].get(
            tile_type, {"width": 1, "height": 1}
        )
        tile_width = tile["width"]
        tile_height = tile["height"]
        if x < 0 or y < 0 or x + tile_width > width or y + tile_height > height:
            return
        for dx in range(tile_width):
            for dy in range(tile_height):
                coordinate = (x + dx, y + dy)
                current = grid.get(coordinate)
                if current is not None and current["priority"] > priority:
                    return
        root = (x, y)
        for dx in range(tile_width):
            for dy in range(tile_height):
                grid[(x + dx, y + dy)] = {
                    "priority": priority,
                    "type": tile_type,
                    "root": root,
                }

    for rule in rules:
        kind = rule["kind"]
        tile_type = rule["type"]
        priority = rule["priority"]
        if kind == "single":
            place(rule["startx"], rule["starty"], tile_type, priority)
        elif kind == "fill":
            for x in range(width):
                for y in range(height):
                    place(x, y, tile_type, priority)
        elif kind == "perimeter":
            for x in range(width):
                place(x, 0, tile_type, priority)
                place(x, height - 1, tile_type, priority)
            for y in range(height):
                place(0, y, tile_type, priority)
                place(width - 1, y, tile_type, priority)
        elif kind == "corners":
            for x, y in (
                (0, 0),
                (0, height - 1),
                (width - 1, 0),
                (width - 1, height - 1),
            ):
                place(x, y, tile_type, priority)
        elif kind == "col":
            startx = max(0, rule["startx"])
            starty = max(0, rule["starty"])
            repeatx = rule["repeatx"] or width
            tile_height = extract["tiles"].get(
                tile_type, {"height": 1}
            )["height"]
            repeaty = rule["repeaty"] or tile_height
            for x in range(startx, width, repeatx):
                for y in range(starty, height, repeaty):
                    place(x, y, tile_type, priority)
        elif kind == "row":
            startx = max(0, rule["startx"])
            starty = max(0, rule["starty"])
            tile_width = extract["tiles"].get(
                tile_type, {"width": 1}
            )["width"]
            repeatx = rule["repeatx"] or tile_width
            repeaty = rule["repeaty"] or height
            for y in range(starty, height, repeaty):
                for x in range(startx, width, repeatx):
                    place(x, y, tile_type, priority)
        else:
            raise ValidationError(f"unsupported VTR layout rule {kind!r}")
    return grid


def _architecture_db(
    extract: Mapping[str, Any],
    *,
    architecture_id: str,
    source_path: Path,
    source_url: Optional[str],
    width: int,
    height: int,
) -> ArchitectureDB:
    templates = _tile_templates(extract)
    grid = _layout_grid(extract, width=width, height=height)
    sites = []
    for (x, y), entry in sorted(grid.items()):
        tile_type = entry["type"]
        if entry["root"] != (x, y) or tile_type not in templates:
            continue
        tile = extract["tiles"][tile_type]
        sites.append(
            {
                "name": f"VTR_{tile_type.upper()}_X{x}Y{y}",
                "type": tile_type,
                "x": x,
                "y": y,
                "template": tile_type,
                "tile": {
                    "width": tile["width"],
                    "height": tile["height"],
                },
            }
        )
    source: Dict[str, Any] = {
        "format": VTR_SOURCE_FORMAT,
        "sha256": _sha256(source_path),
        "generator": "emuflow_vtr_arch_importer",
        "generator_qualification": "open-source",
    }
    if source_url:
        source["url"] = source_url
    return ArchitectureDB(
        {
            "schema": ARCHDB_SCHEMA,
            "part": f"vtr:{architecture_id}:{width}x{height}",
            "source": source,
            "policy": {
                "name": "vtr-relaxed-flat-capacity-v1",
                "description": (
                    "Expose maximum primitive capacities per VTR physical "
                    "block; exact mode-aware packing remains a separate stage."
                ),
            },
            "site_templates": templates,
            "sites": sites,
        }
    )


def _timing_db(
    extract: Mapping[str, Any],
    *,
    architecture: ArchitectureDB,
    architecture_id: str,
    source_path: Path,
    source_url: Optional[str],
) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "format": VTR_SOURCE_FORMAT,
        "sha256": _sha256(source_path),
        "qualification": "academic_open_model",
    }
    if source_url:
        source["url"] = source_url
    return {
        "schema": ARCHITECTURE_TIMING_DB_SCHEMA,
        "architecture": architecture.part,
        "name": architecture_id,
        "source": source,
        "primitives": extract["primitives"],
        "primitive_arcs": extract["primitive_arcs"],
        "block_interconnect_arcs": extract["block_interconnect_arcs"],
        "routing": {
            "switches": extract["switches"],
            "segments": extract["segments"],
            "directs": extract["directs"],
        },
    }


def validate_vtr_timing_db(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != ARCHITECTURE_TIMING_DB_SCHEMA:
        raise ValidationError("Architecture TimingDB schema is invalid")
    for key in ("architecture", "name"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValidationError(f"VTR TimingDB {key} is invalid")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("format") != VTR_SOURCE_FORMAT:
        raise ValidationError("VTR TimingDB source is invalid")
    digest = source.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValidationError("VTR TimingDB source SHA-256 is invalid")
    if source.get("qualification") != "academic_open_model":
        raise ValidationError("VTR TimingDB qualification is invalid")
    primitives = value.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        raise ValidationError("VTR TimingDB contains no primitives")
    primitive_paths = set()
    for index, primitive in enumerate(primitives):
        context = f"VTR TimingDB primitive {index}"
        if not isinstance(primitive, dict):
            raise ValidationError(f"{context} is invalid")
        path = primitive.get("path")
        if not isinstance(path, str) or not path or path in primitive_paths:
            raise ValidationError(f"{context} path is invalid")
        primitive_paths.add(path)
        if not isinstance(primitive.get("cell"), str) or not primitive["cell"]:
            raise ValidationError(f"{context} cell is invalid")
        ports = primitive.get("ports")
        if not isinstance(ports, list):
            raise ValidationError(f"{context} ports are invalid")
    primitive_arcs = value.get("primitive_arcs")
    block_arcs = value.get("block_interconnect_arcs")
    if not isinstance(primitive_arcs, list) or not primitive_arcs:
        raise ValidationError("VTR TimingDB contains no primitive arcs")
    if not isinstance(block_arcs, list) or not block_arcs:
        raise ValidationError("VTR TimingDB contains no block interconnect arcs")
    for index, arc in enumerate([*primitive_arcs, *block_arcs]):
        if not isinstance(arc, dict):
            raise ValidationError(f"VTR TimingDB arc {index} is invalid")
        if not isinstance(arc.get("scope"), str) or not arc["scope"]:
            raise ValidationError(
                f"VTR TimingDB arc {index} scope is invalid"
            )
        for field in ("min_seconds", "max_seconds"):
            value_number = arc.get(field)
            if (
                isinstance(value_number, bool)
                or not isinstance(value_number, (int, float))
                or not math.isfinite(float(value_number))
                or float(value_number) < 0.0
            ):
                raise ValidationError(
                    f"VTR TimingDB arc {index} {field} is invalid"
                )
    routing = value.get("routing")
    if not isinstance(routing, dict):
        raise ValidationError("VTR TimingDB routing is invalid")
    for key in ("switches", "segments", "directs"):
        if not isinstance(routing.get(key), list):
            raise ValidationError(f"VTR TimingDB routing {key} is invalid")
    if not routing["switches"] or not routing["segments"]:
        raise ValidationError("VTR TimingDB routing model is empty")
    cells = sorted({primitive["cell"] for primitive in primitives})
    return {
        "status": "pass",
        "architecture": value["architecture"],
        "primitive_cells": cells,
        "primitive_arcs": len(primitive_arcs),
        "block_interconnect_arcs": len(block_arcs),
        "switches": len(routing["switches"]),
        "segments": len(routing["segments"]),
        "directs": len(routing["directs"]),
        "qualification": source["qualification"],
    }


def validate_vtr_timing_db_file(path: Path) -> Dict[str, Any]:
    return validate_vtr_timing_db(read_json(path))


def validate_vtr_architecture_db(
    architecture: ArchitectureDB,
) -> Dict[str, Any]:
    source = architecture.value.get("source", {})
    if source.get("format") != VTR_SOURCE_FORMAT:
        raise ValidationError("ArchitectureDB is not sourced from VTR XML")
    if architecture.value["policy"].get("name") != (
        "vtr-relaxed-flat-capacity-v1"
    ):
        raise ValidationError("VTR ArchitectureDB policy is invalid")
    summary = architecture.summary()
    if not summary["cell_slots"]:
        raise ValidationError("VTR ArchitectureDB contains no cell slots")
    cluster_capacity: Counter[str] = Counter()
    for template_name, template in architecture.value.get(
        "site_templates", {}
    ).items():
        raw_capacity = template.get("vtr_cluster_capacity")
        if not isinstance(raw_capacity, dict) or not raw_capacity:
            raise ValidationError(
                f"VTR site template {template_name!r} has no cluster capacity"
            )
        for block_type, capacity in raw_capacity.items():
            if (
                not isinstance(block_type, str)
                or not block_type
                or isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity <= 0
            ):
                raise ValidationError(
                    f"VTR site template {template_name!r} cluster capacity "
                    "is invalid"
                )
            cluster_capacity[block_type] += capacity
    return {
        "status": "pass",
        **summary,
        "cluster_types": dict(sorted(cluster_capacity.items())),
    }


def run_vtr_architecture_import(
    *,
    input_path: Path,
    architecture_output_path: Path,
    timing_output_path: Path,
    architecture_id: str,
    width: int,
    height: int,
    source_url: Optional[str] = None,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    if not architecture_id:
        raise ValidationError("VTR architecture id must be non-empty")
    importer = resolve_native_executable(
        "emuflow_vtr_arch_importer", executable
    )
    completed = subprocess.run(
        [importer, str(input_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise ValidationError(
            "VTR architecture importer failed with exit code "
            f"{completed.returncode}: {completed.stdout.strip()}"
        )
    extract = _parse_extract(completed.stdout)
    architecture = _architecture_db(
        extract,
        architecture_id=architecture_id,
        source_path=input_path,
        source_url=source_url,
        width=width,
        height=height,
    )
    timing = _timing_db(
        extract,
        architecture=architecture,
        architecture_id=architecture_id,
        source_path=input_path,
        source_url=source_url,
    )
    architecture_report = validate_vtr_architecture_db(architecture)
    timing_report = validate_vtr_timing_db(timing)
    write_json(architecture_output_path, architecture.to_dict())
    write_json(timing_output_path, timing)
    return {
        "status": "pass",
        "architecture": architecture_report,
        "timing": timing_report,
    }
