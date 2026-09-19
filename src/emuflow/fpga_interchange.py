"""Open FPGA Interchange DeviceResources to ArchitectureDB conversion."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .architecture import ARCHDB_SCHEMA, ArchitectureDB
from .errors import EmuFlowError, ValidationError
from .ir import EmuIR
from .io import read_json, write_json
from .native_tools import resolve_native_executable


FPGAIF_ARCH_EXTRACT_SCHEMA = (
    "emuflow.fpga-interchange-architecture-extract/v1"
)
FPGAIF_ARCH_SOURCE_FORMAT = "fpga-interchange-device-resources/v1"
FPGAIF_ARCH_POLICY = "fpga-interchange-ultrascaleplus-v1"
_XILINX_PART_RE = re.compile(
    r"^(?P<device>xc[a-z0-9]+)-(?P<package>[a-z0-9]+)-"
    r"(?P<speed>[0-9][a-z0-9]*)-(?P<temperature>[a-z])$",
    re.IGNORECASE,
)
SUPPORTED_PLACEMENT_CELLS = {
    "CARRY8",
    "DSP48E2",
    "FDCE",
    "FDPE",
    "FDRE",
    "FDSE",
    "LUT1",
    "LUT2",
    "LUT3",
    "LUT4",
    "LUT5",
    "LUT6",
    "MUXF7",
    "MUXF8",
    "RAM64X1S",
    "RAMB18E2",
    "RAMB36E2",
    "URAM288",
}
NON_PLACEMENT_CELLS = {"GND", "VCC"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _nonnegative_integer(value: Any, context: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValidationError(f"{context}: expected a non-negative integer")
    return value


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def parse_xilinx_part_identity(part: str) -> Dict[str, str]:
    """Parse one complete AMD/Xilinx part identity without guessing fields."""
    match = _XILINX_PART_RE.fullmatch(_nonempty_string(part, "part"))
    if match is None:
        raise ValidationError(
            "part: expected a complete device-package-speed-temperature "
            "identity such as xcvu19p-fsva3824-2-e"
        )
    fields = {key: value.lower() for key, value in match.groupdict().items()}
    return {
        **fields,
        "part": part.lower(),
        "speed_grade": f"-{fields['speed'].upper()}",
        "temperature_grade": fields["temperature"].upper(),
        "grade": (
            f"-{fields['speed'].upper()}-{fields['temperature'].lower()}"
        ),
    }


def _select_part_package(
    packages: Any, identity: Mapping[str, str]
) -> Dict[str, Any]:
    if not isinstance(packages, list) or not packages:
        raise ValidationError("extract.packages: expected a non-empty array")
    seen_packages = set()
    selected = None
    for package_index, package in enumerate(packages):
        context = f"extract.packages[{package_index}]"
        if not isinstance(package, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _nonempty_string(package.get("name"), f"{context}.name")
        normalized_name = name.lower()
        if normalized_name in seen_packages:
            raise ValidationError(f"{context}.name: duplicate package {name!r}")
        seen_packages.add(normalized_name)
        pin_count = _nonnegative_integer(
            package.get("package_pin_count"),
            f"{context}.package_pin_count",
        )
        if pin_count == 0:
            raise ValidationError(
                f"{context}.package_pin_count: expected a positive integer"
            )
        grades = package.get("grades")
        if not isinstance(grades, list) or not grades:
            raise ValidationError(f"{context}.grades: expected a non-empty array")
        seen_grades = set()
        matching_grade = None
        for grade_index, grade in enumerate(grades):
            grade_context = f"{context}.grades[{grade_index}]"
            if not isinstance(grade, dict):
                raise ValidationError(f"{grade_context}: expected an object")
            grade_name = _nonempty_string(
                grade.get("name"), f"{grade_context}.name"
            )
            speed_grade = _nonempty_string(
                grade.get("speed_grade"), f"{grade_context}.speed_grade"
            )
            temperature_grade = _nonempty_string(
                grade.get("temperature_grade"),
                f"{grade_context}.temperature_grade",
            )
            grade_key = (
                grade_name.lower(),
                speed_grade.upper(),
                temperature_grade.upper(),
            )
            if grade_key in seen_grades:
                raise ValidationError(
                    f"{grade_context}: duplicate package grade {grade_name!r}"
                )
            seen_grades.add(grade_key)
            if (
                speed_grade.upper() == identity["speed_grade"]
                and temperature_grade.upper()
                == identity["temperature_grade"]
            ):
                matching_grade = {
                    "name": grade_name,
                    "speed_grade": speed_grade,
                    "temperature_grade": temperature_grade,
                }
        if normalized_name == identity["package"]:
            if matching_grade is None:
                raise ValidationError(
                    f"{context}: package {name!r} does not provide requested "
                    f"grade {identity['speed_grade']}/"
                    f"{identity['temperature_grade']}"
                )
            selected = {
                "name": name,
                "package_pin_count": pin_count,
                "grade": matching_grade,
            }
    if selected is None:
        raise ValidationError(
            "extract.packages: requested package "
            f"{identity['package']!r} is absent"
        )
    return selected


def architecture_from_fpga_interchange_extract(
    extract: Mapping[str, Any],
    *,
    part: str,
    input_path: Path,
    generator: str,
    generator_qualification: str = "declared-not-assumed-open",
    producer: Optional[Mapping[str, Any]] = None,
) -> ArchitectureDB:
    if extract.get("schema") != FPGAIF_ARCH_EXTRACT_SCHEMA:
        raise ValidationError(
            "FPGA Interchange architecture extract schema is invalid"
        )
    identity = parse_xilinx_part_identity(part)
    device = _nonempty_string(extract.get("device"), "extract.device")
    if device.lower() != identity["device"]:
        raise ValidationError(
            "extract.device does not match the complete requested part"
        )
    _nonempty_string(generator, "generator")
    _nonempty_string(generator_qualification, "generator_qualification")
    raw_tiles = extract.get("tiles")
    if not isinstance(raw_tiles, list) or not raw_tiles:
        raise ValidationError("extract.tiles: expected a non-empty array")
    raw_templates = extract.get("site_templates", {})
    if not isinstance(raw_templates, dict):
        raise ValidationError("extract.site_templates: expected an object")

    def normalize_bels(raw_bels: Any, context: str) -> list[Dict[str, Any]]:
        if not isinstance(raw_bels, list) or not raw_bels:
            raise ValidationError(f"{context}: expected a non-empty array")
        result = []
        for bel_index, raw_bel in enumerate(raw_bels):
            bel_context = f"{context}[{bel_index}]"
            if not isinstance(raw_bel, dict):
                raise ValidationError(f"{bel_context}: expected an object")
            cells = raw_bel.get("compatible_cells")
            if (
                not isinstance(cells, list)
                or not cells
                or not all(isinstance(cell, str) and cell for cell in cells)
                or len(cells) != len(set(cells))
                or not set(cells) <= SUPPORTED_PLACEMENT_CELLS
            ):
                raise ValidationError(
                    f"{bel_context}.compatible_cells: invalid cells"
                )
            result.append(
                {
                    "name": _nonempty_string(
                        raw_bel.get("name"), f"{bel_context}.name"
                    ),
                    "type": _nonempty_string(
                        raw_bel.get("type"), f"{bel_context}.type"
                    ),
                    "z": _nonnegative_integer(
                        raw_bel.get("z"), f"{bel_context}.z"
                    ),
                    "compatible_cells": sorted(cells),
                }
            )
        return result

    site_templates: Dict[str, Dict[str, Any]] = {}
    for template_name, raw_template in raw_templates.items():
        context = f"extract.site_templates[{template_name!r}]"
        _nonempty_string(template_name, "extract.site_templates key")
        if not isinstance(raw_template, dict):
            raise ValidationError(f"{context}: expected an object")
        alternatives = raw_template.get("alternative_templates", [])
        if (
            not isinstance(alternatives, list)
            or not all(
                isinstance(alternative, str) and alternative
                for alternative in alternatives
            )
            or len(alternatives) != len(set(alternatives))
        ):
            raise ValidationError(
                f"{context}.alternative_templates: invalid templates"
            )
        site_templates[template_name] = {
            "bels": normalize_bels(raw_template.get("bels"), f"{context}.bels"),
            "alternative_templates": sorted(alternatives),
        }
    for template_name, template in site_templates.items():
        unknown = set(template["alternative_templates"]) - set(site_templates)
        if unknown:
            raise ValidationError(
                f"extract.site_templates[{template_name!r}]: unknown "
                f"alternative templates {sorted(unknown)}"
            )

    max_site_index = 0
    for tile_index, tile in enumerate(raw_tiles):
        context = f"extract.tiles[{tile_index}]"
        if not isinstance(tile, dict):
            raise ValidationError(f"{context}: expected an object")
        _nonempty_string(tile.get("name"), f"{context}.name")
        _nonempty_string(tile.get("type"), f"{context}.type")
        _nonnegative_integer(tile.get("row"), f"{context}.row")
        _nonnegative_integer(tile.get("col"), f"{context}.col")
        raw_sites = tile.get("sites")
        if not isinstance(raw_sites, list) or not raw_sites:
            raise ValidationError(f"{context}.sites: expected a non-empty array")
        for site_index, site in enumerate(raw_sites):
            site_context = f"{context}.sites[{site_index}]"
            if not isinstance(site, dict):
                raise ValidationError(f"{site_context}: expected an object")
            site_type = _nonempty_string(
                site.get("type"), f"{site_context}.type"
            )
            if "bels" not in site and site_type not in site_templates:
                raise ValidationError(
                    f"{site_context}: missing BELs and site template"
                )
            max_site_index = max(
                max_site_index,
                _nonnegative_integer(
                    site.get("index_in_tile"),
                    f"{site_context}.index_in_tile",
                ),
            )
    site_stride = max_site_index + 1

    sites = []
    site_names = set()
    coordinates = set()
    for tile_index, tile in enumerate(raw_tiles):
        for site_index, raw_site in enumerate(tile["sites"]):
            context = f"extract.tiles[{tile_index}].sites[{site_index}]"
            site_name = _nonempty_string(
                raw_site.get("name"), f"{context}.name"
            )
            if site_name in site_names:
                raise ValidationError(f"{context}.name: duplicate site {site_name!r}")
            site_names.add(site_name)
            index_in_tile = _nonnegative_integer(
                raw_site.get("index_in_tile"),
                f"{context}.index_in_tile",
            )
            x = int(tile["col"]) * site_stride + index_in_tile
            y = int(tile["row"])
            if (x, y) in coordinates:
                raise ValidationError(
                    f"{context}: duplicate transformed coordinate {(x, y)}"
                )
            coordinates.add((x, y))

            site_type = _nonempty_string(
                raw_site.get("type"), f"{context}.type"
            )
            if "bels" in raw_site:
                bels = normalize_bels(raw_site["bels"], f"{context}.bels")
                existing = site_templates.get(site_type)
                if existing is not None and existing["bels"] != bels:
                    raise ValidationError(
                        f"{context}: inconsistent BELs for site type {site_type}"
                    )
                site_templates.setdefault(
                    site_type,
                    {"bels": bels, "alternative_templates": []},
                )
            sites.append(
                {
                    "name": site_name,
                    "type": site_type,
                    "x": x,
                    "y": y,
                    "template": site_type,
                    "tile": {
                        "name": tile["name"],
                        "type": tile["type"],
                        "grid_col": tile["col"],
                        "grid_row": tile["row"],
                        "site_index": index_in_tile,
                    },
                    "physical_region": {
                        "slr": None,
                        "clock_region": None,
                        "qualification": (
                            "not-encoded-by-fpga-interchange-device-resources-v1"
                        ),
                    },
                }
            )

    packages = extract.get("packages")
    selected_package = _select_part_package(packages, identity)
    resource_counts = extract.get("resource_counts")
    if not isinstance(resource_counts, dict):
        raise ValidationError("extract.resource_counts: expected an object")
    for key, value in resource_counts.items():
        _nonempty_string(key, "extract.resource_counts key")
        _nonnegative_integer(value, f"extract.resource_counts[{key!r}]")
    reference_integrity = extract.get("reference_integrity")
    if not isinstance(reference_integrity, dict):
        raise ValidationError(
            "extract.reference_integrity: expected an object"
        )
    if reference_integrity.get("status") != "pass":
        raise ValidationError(
            "extract.reference_integrity.status: expected 'pass'"
        )
    for key in (
        "site_types",
        "tile_types",
        "tiles",
        "wires",
        "nodes",
        "pips",
    ):
        _nonnegative_integer(
            reference_integrity.get(key),
            f"extract.reference_integrity.{key}",
        )
    for integrity_key, count_key in (
        ("site_types", "site_types"),
        ("tile_types", "tile_types"),
        ("tiles", "all_tiles"),
        ("wires", "wires"),
        ("nodes", "nodes"),
    ):
        if reference_integrity[integrity_key] != resource_counts[count_key]:
            raise ValidationError(
                "extract.reference_integrity does not match resource_counts"
            )

    source: Dict[str, Any] = {
        "format": FPGAIF_ARCH_SOURCE_FORMAT,
        "device": device,
        "path": str(input_path),
        "sha256": _sha256(input_path),
        "generator": generator,
        "schema_license": "Apache-2.0",
        "generator_qualification": generator_qualification,
        "device_identity": {
            **identity,
            "selected_package_pin_count": selected_package[
                "package_pin_count"
            ],
        },
    }
    if producer is not None:
        if not isinstance(producer, Mapping):
            raise ValidationError("producer: expected an object")
        source["producer"] = dict(producer)

    architecture = ArchitectureDB(
        {
            "schema": ARCHDB_SCHEMA,
            "part": identity["part"],
            "source": source,
            "policy": {
                "name": FPGAIF_ARCH_POLICY,
                "description": (
                    "Use FPGA Interchange cell-to-BEL mappings for supported "
                    "UltraScale+ LUT, FF, carry, DSP, BRAM, distributed RAM, "
                    "and URAM primitives; exclude shared 5LUT BELs; represent "
                    "DSP48E2 by its macro's DSP_ALU component and the BRAM "
                    "RAMB180/RAMB181/RAMB36 mutually related packing modes."
                ),
            },
            "coordinate_transform": {
                "name": "tile-grid-with-site-stride-v1",
                "site_stride": site_stride,
                "x_formula": "tile_col * site_stride + site_index",
                "y_formula": "tile_row",
            },
            "physical_region_model": {
                "qualification": (
                    "not-encoded-by-fpga-interchange-device-resources-v1"
                ),
                "slr_encoded": False,
                "clock_region_encoded": False,
                "io_bank_encoded": False,
            },
            "packages": packages,
            "routing_resource_counts": dict(sorted(resource_counts.items())),
            "routing_reference_integrity": dict(
                sorted(reference_integrity.items())
            ),
            "site_templates": dict(sorted(site_templates.items())),
            "sites": sites,
        }
    )
    validate_fpga_interchange_architecture(architecture)
    return architecture


def validate_fpga_interchange_architecture(
    architecture: ArchitectureDB,
) -> Dict[str, Any]:
    value = architecture.value
    if value["source"].get("format") != FPGAIF_ARCH_SOURCE_FORMAT:
        raise ValidationError("ArchitectureDB is not sourced from FPGA Interchange")
    identity = parse_xilinx_part_identity(value["part"])
    source_identity = value["source"].get("device_identity")
    if not isinstance(source_identity, dict):
        raise ValidationError("ArchitectureDB source device identity is missing")
    for field in (
        "part",
        "device",
        "package",
        "speed_grade",
        "temperature_grade",
    ):
        if source_identity.get(field) != identity[field]:
            raise ValidationError(
                f"ArchitectureDB source device identity field {field!r} "
                "does not match arch.part"
            )
    selected_package = _select_part_package(value.get("packages"), identity)
    if source_identity.get("selected_package_pin_count") != selected_package[
        "package_pin_count"
    ]:
        raise ValidationError(
            "ArchitectureDB selected package pin count is inconsistent"
        )
    transform = value.get("coordinate_transform")
    if not isinstance(transform, dict):
        raise ValidationError("ArchitectureDB coordinate transform is missing")
    reference_integrity = value.get("routing_reference_integrity")
    if (
        not isinstance(reference_integrity, dict)
        or reference_integrity.get("status") != "pass"
    ):
        raise ValidationError(
            "ArchitectureDB routing reference integrity is missing"
        )
    resource_counts = value.get("routing_resource_counts")
    if not isinstance(resource_counts, dict):
        raise ValidationError(
            "ArchitectureDB routing resource counts are missing"
        )
    for integrity_key, count_key in (
        ("site_types", "site_types"),
        ("tile_types", "tile_types"),
        ("tiles", "all_tiles"),
        ("wires", "wires"),
        ("nodes", "nodes"),
    ):
        if reference_integrity.get(integrity_key) != resource_counts.get(
            count_key
        ):
            raise ValidationError(
                "ArchitectureDB routing reference integrity is inconsistent"
            )
    stride = _nonnegative_integer(
        transform.get("site_stride"), "arch.coordinate_transform.site_stride"
    )
    if stride <= 0:
        raise ValidationError("ArchitectureDB site stride must be positive")
    region_model = value.get("physical_region_model")
    if not isinstance(region_model, dict):
        raise ValidationError("ArchitectureDB physical-region model is missing")
    for field in ("slr_encoded", "clock_region_encoded", "io_bank_encoded"):
        if not isinstance(region_model.get(field), bool):
            raise ValidationError(
                f"arch.physical_region_model.{field}: expected a boolean"
            )
    if region_model["slr_encoded"] != region_model["clock_region_encoded"]:
        raise ValidationError(
            "ArchitectureDB must encode SLR and clock-region membership "
            "together"
        )

    for index, site in enumerate(value["sites"]):
        context = f"arch.sites[{index}]"
        tile = site.get("tile")
        if not isinstance(tile, dict):
            raise ValidationError(f"{context}.tile is missing")
        col = _nonnegative_integer(
            tile.get("grid_col"), f"{context}.tile.grid_col"
        )
        row = _nonnegative_integer(
            tile.get("grid_row"), f"{context}.tile.grid_row"
        )
        site_index = _nonnegative_integer(
            tile.get("site_index"), f"{context}.tile.site_index"
        )
        physical_region = site.get("physical_region")
        if not isinstance(physical_region, dict):
            raise ValidationError(f"{context}.physical_region: missing")
        qualified = region_model["slr_encoded"]
        slr = physical_region.get("slr")
        clock_region = physical_region.get("clock_region")
        if qualified and (
            not isinstance(slr, str)
            or not slr
            or not isinstance(clock_region, str)
            or not clock_region
        ):
            raise ValidationError(
                f"{context}.physical_region: expected qualified regions"
            )
        if not qualified and (slr is not None or clock_region is not None):
            raise ValidationError(
                f"{context}.physical_region: expected unqualified regions"
            )
        if site["x"] != col * stride + site_index or site["y"] != row:
            raise ValidationError(f"{context}: coordinate transform mismatch")
    resource_sites = Counter(architecture.summary()["cell_slots"])
    if not resource_sites:
        raise ValidationError("ArchitectureDB contains no supported resources")
    report = {
        "status": "pass",
        "part": value["part"],
        "device": value["source"]["device"],
        "device_identity": dict(source_identity),
        "sites": len(value["sites"]),
        "cell_slots": dict(sorted(resource_sites.items())),
        "routing_reference_integrity": dict(reference_integrity),
        "physical_region_qualification": region_model.get("qualification"),
    }
    if region_model["slr_encoded"]:
        from .physical_regions import (
            validate_fpga_interchange_architecture_regions,
        )

        report["physical_regions"] = (
            validate_fpga_interchange_architecture_regions(architecture)
        )
    return report


def check_ir_architecture_capacity(
    architecture: ArchitectureDB,
    ir: EmuIR,
) -> Dict[str, Any]:
    """Check primitive support and scalar BEL capacity without placing cells."""
    available = Counter(architecture.summary()["cell_slots"])
    required = Counter(
        instance["type"]
        for instance in ir.value["instances"]
        if instance["type"] not in NON_PLACEMENT_CELLS
    )
    unsupported = {
        cell: count
        for cell, count in required.items()
        if cell not in SUPPORTED_PLACEMENT_CELLS
    }
    overflow = {
        cell: {
            "required": count,
            "available": available.get(cell, 0),
            "excess": count - available.get(cell, 0),
        }
        for cell, count in required.items()
        if cell in SUPPORTED_PLACEMENT_CELLS
        and count > available.get(cell, 0)
    }
    report = {
        "status": "pass" if not unsupported and not overflow else "fail",
        "part": architecture.part,
        "design": ir.value["design"]["name"],
        "instances": len(ir.value["instances"]),
        "required_cell_slots": dict(sorted(required.items())),
        "available_cell_slots": {
            cell: available.get(cell, 0) for cell in sorted(required)
        },
        "unsupported_cell_types": dict(sorted(unsupported.items())),
        "capacity_overflow": dict(sorted(overflow.items())),
    }
    return report


def run_fpga_interchange_architecture_import(
    *,
    input_path: Path,
    part: str,
    generator: str,
    generator_qualification: str = "declared-not-assumed-open",
    producer: Optional[Mapping[str, Any]] = None,
    output_path: Path,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    importer = resolve_native_executable(
        "emuflow_fpgaif_arch_importer", executable
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="emuflow-fpgaif-") as temporary:
        extract_path = Path(temporary) / "architecture-extract.json"
        completed = subprocess.run(
            [importer, str(input_path), str(extract_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(completed.stdout, encoding="utf-8")
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-30:])
            raise EmuFlowError(
                "FPGA Interchange architecture extraction failed with exit "
                f"code {completed.returncode}\n{tail}"
            )
        if not extract_path.is_file():
            raise EmuFlowError(
                "FPGA Interchange importer did not create its extract"
            )
        architecture = architecture_from_fpga_interchange_extract(
            read_json(extract_path),
            part=part,
            input_path=input_path,
            generator=generator,
            generator_qualification=generator_qualification,
            producer=producer,
        )
        write_json(output_path, architecture.to_dict())
    checked = validate_fpga_interchange_architecture(
        ArchitectureDB.load(output_path)
    )
    return {
        **architecture.summary(),
        "status": "pass",
        "source": FPGAIF_ARCH_SOURCE_FORMAT,
        "checker": checked,
        "output": str(output_path),
        "log": str(log_path) if log_path is not None else None,
    }
