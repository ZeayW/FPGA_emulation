"""Source-qualified physical-region overlays for ArchitectureDB."""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json


PHYSICAL_REGION_SIDECAR_SCHEMA = "emuflow.physical-region-sidecar/v1"
PHYSICAL_REGION_BINDING_FIELDS = (
    "part",
    "coordinate_transform",
    "packages",
    "physical_region_model",
    "site_templates",
    "sites",
)


def _physical_region_binding_value(
    architecture: ArchitectureDB, field: str
) -> Any:
    value = architecture.value.get(field)
    if field != "site_templates" or not isinstance(value, dict):
        return value
    templates = copy.deepcopy(value)
    for template in templates.values():
        if not isinstance(template, dict):
            continue
        for bel in template.get("bels", []):
            if isinstance(bel, dict):
                bel.pop("compatible_cells", None)
    return templates


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context}: expected a non-negative integer")
    return value


def validate_physical_region_sidecar(
    architecture: ArchitectureDB,
    sidecar: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate exact coverage and referential integrity before merging."""
    if sidecar.get("schema") != PHYSICAL_REGION_SIDECAR_SCHEMA:
        raise ValidationError("physical-region sidecar schema is invalid")
    if sidecar.get("part") != architecture.part:
        raise ValidationError(
            "physical-region sidecar part does not match ArchitectureDB"
        )
    source = sidecar.get("source")
    if not isinstance(source, dict):
        raise ValidationError("physical-region sidecar source is missing")
    for field in ("producer", "producer_version", "qualification"):
        _string(source.get(field), f"sidecar.source.{field}")

    raw_slrs = sidecar.get("slrs")
    if not isinstance(raw_slrs, list) or not raw_slrs:
        raise ValidationError("sidecar.slrs: expected a non-empty array")
    slrs: Dict[str, Mapping[str, Any]] = {}
    slr_indices = set()
    for index, raw_slr in enumerate(raw_slrs):
        context = f"sidecar.slrs[{index}]"
        if not isinstance(raw_slr, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(raw_slr.get("name"), f"{context}.name")
        slr_index = _integer(raw_slr.get("index"), f"{context}.index")
        if name in slrs or slr_index in slr_indices:
            raise ValidationError(f"{context}: duplicate SLR name or index")
        slrs[name] = raw_slr
        slr_indices.add(slr_index)

    raw_clock_regions = sidecar.get("clock_regions")
    if not isinstance(raw_clock_regions, list) or not raw_clock_regions:
        raise ValidationError(
            "sidecar.clock_regions: expected a non-empty array"
        )
    clock_regions: Dict[str, Mapping[str, Any]] = {}
    for index, raw_region in enumerate(raw_clock_regions):
        context = f"sidecar.clock_regions[{index}]"
        if not isinstance(raw_region, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(raw_region.get("name"), f"{context}.name")
        slr = _string(raw_region.get("slr"), f"{context}.slr")
        if slr not in slrs:
            raise ValidationError(f"{context}.slr: unknown SLR {slr!r}")
        _integer(raw_region.get("grid_x"), f"{context}.grid_x")
        _integer(raw_region.get("grid_y"), f"{context}.grid_y")
        if name in clock_regions:
            raise ValidationError(f"{context}.name: duplicate clock region")
        clock_regions[name] = raw_region

    architecture_sites = {
        site["name"] for site in architecture.value["sites"]
    }
    assignments: Dict[str, tuple[str, str]] = {}
    raw_groups = sidecar.get("site_region_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValidationError(
            "sidecar.site_region_groups: expected a non-empty array"
        )
    group_keys = set()
    for index, raw_group in enumerate(raw_groups):
        context = f"sidecar.site_region_groups[{index}]"
        if not isinstance(raw_group, dict):
            raise ValidationError(f"{context}: expected an object")
        slr = _string(raw_group.get("slr"), f"{context}.slr")
        clock_region = _string(
            raw_group.get("clock_region"), f"{context}.clock_region"
        )
        if slr not in slrs:
            raise ValidationError(f"{context}.slr: unknown SLR {slr!r}")
        region = clock_regions.get(clock_region)
        if region is None:
            raise ValidationError(
                f"{context}.clock_region: unknown region {clock_region!r}"
            )
        if region["slr"] != slr:
            raise ValidationError(
                f"{context}: clock region belongs to a different SLR"
            )
        if (slr, clock_region) in group_keys:
            raise ValidationError(f"{context}: duplicate region group")
        group_keys.add((slr, clock_region))
        sites = raw_group.get("sites")
        if (
            not isinstance(sites, list)
            or not sites
            or not all(isinstance(site, str) and site for site in sites)
        ):
            raise ValidationError(f"{context}.sites: expected site names")
        for site in sites:
            if site not in architecture_sites:
                raise ValidationError(
                    f"{context}.sites: unknown ArchitectureDB site {site!r}"
                )
            if site in assignments:
                raise ValidationError(
                    f"{context}.sites: duplicate assignment for {site!r}"
                )
            assignments[site] = (slr, clock_region)
    missing = architecture_sites - set(assignments)
    if missing:
        sample = sorted(missing)[:5]
        raise ValidationError(
            "physical-region sidecar does not cover every ArchitectureDB "
            f"site; missing {len(missing)} (sample: {sample})"
        )

    raw_banks = sidecar.get("io_banks")
    if not isinstance(raw_banks, list):
        raise ValidationError("sidecar.io_banks: expected an array")
    bank_names = set()
    package_pins = set()
    io_site_to_banks: Dict[str, set[str]] = {}
    pin_count = 0
    for bank_index, raw_bank in enumerate(raw_banks):
        context = f"sidecar.io_banks[{bank_index}]"
        if not isinstance(raw_bank, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(raw_bank.get("name"), f"{context}.name")
        _string(raw_bank.get("bank_type"), f"{context}.bank_type")
        _string(raw_bank.get("package"), f"{context}.package")
        if name in bank_names:
            raise ValidationError(f"{context}.name: duplicate I/O bank")
        bank_names.add(name)
        pins = raw_bank.get("pins")
        if not isinstance(pins, list):
            raise ValidationError(f"{context}.pins: expected an array")
        for pin_index, raw_pin in enumerate(pins):
            pin_context = f"{context}.pins[{pin_index}]"
            if not isinstance(raw_pin, dict):
                raise ValidationError(f"{pin_context}: expected an object")
            package_pin = _string(
                raw_pin.get("package_pin"), f"{pin_context}.package_pin"
            )
            site = _string(raw_pin.get("site"), f"{pin_context}.site")
            _string(raw_pin.get("bel"), f"{pin_context}.bel")
            if package_pin in package_pins:
                raise ValidationError(
                    f"{pin_context}: duplicate package pin {package_pin!r}"
                )
            package_pins.add(package_pin)
            io_site_to_banks.setdefault(site, set()).add(name)
            pin_count += 1

    return {
        "status": "pass",
        "part": architecture.part,
        "sites": len(assignments),
        "slrs": len(slrs),
        "clock_regions": len(clock_regions),
        "io_banks": len(raw_banks),
        "package_pins": pin_count,
        "assignments": assignments,
        "io_site_to_banks": io_site_to_banks,
        "qualification": source["qualification"],
    }


def merge_physical_regions(
    architecture: ArchitectureDB,
    sidecar: Mapping[str, Any],
    *,
    sidecar_path: Path,
) -> ArchitectureDB:
    checked = validate_physical_region_sidecar(architecture, sidecar)
    value = copy.deepcopy(architecture.value)
    assignments = checked["assignments"]
    io_site_to_banks = checked["io_site_to_banks"]
    qualification = checked["qualification"]
    for site in value["sites"]:
        slr, clock_region = assignments[site["name"]]
        region = {
            "slr": slr,
            "clock_region": clock_region,
            "qualification": qualification,
        }
        banks = io_site_to_banks.get(site["name"], set())
        if len(banks) == 1:
            region["io_bank"] = next(iter(banks))
        site["physical_region"] = region

    value["physical_region_model"] = {
        "qualification": qualification,
        "slr_encoded": True,
        "clock_region_encoded": True,
        "io_bank_encoded": bool(sidecar["io_banks"]),
        "overlay": {
            "schema": PHYSICAL_REGION_SIDECAR_SCHEMA,
            "path": str(sidecar_path),
            "sha256": _sha256(sidecar_path),
            "producer": sidecar["source"]["producer"],
            "producer_version": sidecar["source"]["producer_version"],
        },
    }
    value["physical_regions"] = {
        "slrs": copy.deepcopy(sidecar["slrs"]),
        "clock_regions": copy.deepcopy(sidecar["clock_regions"]),
        "io_banks": copy.deepcopy(sidecar["io_banks"]),
    }
    merged = ArchitectureDB(value)
    validate_fpga_interchange_architecture_regions(merged)
    return merged


def validate_fpga_interchange_architecture_regions(
    architecture: ArchitectureDB,
) -> Dict[str, Any]:
    model = architecture.value.get("physical_region_model")
    if not isinstance(model, dict):
        raise ValidationError("ArchitectureDB physical-region model is missing")
    if model.get("slr_encoded") is not True:
        raise ValidationError("ArchitectureDB does not encode SLR membership")
    if model.get("clock_region_encoded") is not True:
        raise ValidationError(
            "ArchitectureDB does not encode clock-region membership"
        )
    regions = architecture.value.get("physical_regions")
    if not isinstance(regions, dict):
        raise ValidationError("ArchitectureDB physical-region catalogs missing")
    raw_slrs = regions.get("slrs")
    if not isinstance(raw_slrs, list) or not raw_slrs:
        raise ValidationError("ArchitectureDB SLR catalog is empty")
    slrs = set()
    for index, value in enumerate(raw_slrs):
        context = f"arch.physical_regions.slrs[{index}]"
        if not isinstance(value, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(value.get("name"), f"{context}.name")
        _integer(value.get("index"), f"{context}.index")
        if name in slrs:
            raise ValidationError(f"{context}.name: duplicate SLR")
        slrs.add(name)

    raw_clock_regions = regions.get("clock_regions")
    if not isinstance(raw_clock_regions, list) or not raw_clock_regions:
        raise ValidationError("ArchitectureDB clock-region catalog is empty")
    clock_to_slr = {}
    for index, value in enumerate(raw_clock_regions):
        context = f"arch.physical_regions.clock_regions[{index}]"
        if not isinstance(value, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(value.get("name"), f"{context}.name")
        slr = _string(value.get("slr"), f"{context}.slr")
        _integer(value.get("grid_x"), f"{context}.grid_x")
        _integer(value.get("grid_y"), f"{context}.grid_y")
        if slr not in slrs:
            raise ValidationError(f"{context}.slr: unknown SLR {slr!r}")
        if name in clock_to_slr:
            raise ValidationError(f"{context}.name: duplicate clock region")
        clock_to_slr[name] = slr

    raw_banks = regions.get("io_banks")
    if not isinstance(raw_banks, list):
        raise ValidationError("ArchitectureDB I/O-bank catalog is invalid")
    bank_names = set()
    package_pins = set()
    for bank_index, bank in enumerate(raw_banks):
        context = f"arch.physical_regions.io_banks[{bank_index}]"
        if not isinstance(bank, dict):
            raise ValidationError(f"{context}: expected an object")
        name = _string(bank.get("name"), f"{context}.name")
        _string(bank.get("bank_type"), f"{context}.bank_type")
        _string(bank.get("package"), f"{context}.package")
        if name in bank_names:
            raise ValidationError(f"{context}.name: duplicate I/O bank")
        bank_names.add(name)
        pins = bank.get("pins")
        if not isinstance(pins, list):
            raise ValidationError(f"{context}.pins: expected an array")
        for pin_index, pin in enumerate(pins):
            pin_context = f"{context}.pins[{pin_index}]"
            if not isinstance(pin, dict):
                raise ValidationError(f"{pin_context}: expected an object")
            package_pin = _string(
                pin.get("package_pin"), f"{pin_context}.package_pin"
            )
            _string(pin.get("site"), f"{pin_context}.site")
            _string(pin.get("bel"), f"{pin_context}.bel")
            if package_pin in package_pins:
                raise ValidationError(
                    f"{pin_context}: duplicate package pin {package_pin!r}"
                )
            package_pins.add(package_pin)

    if model["io_bank_encoded"] != bool(raw_banks):
        raise ValidationError(
            "ArchitectureDB I/O-bank model flag/catalog mismatch"
        )
    counts: Counter[str] = Counter()
    for index, site in enumerate(architecture.value["sites"]):
        context = f"arch.sites[{index}].physical_region"
        region = site.get("physical_region")
        if not isinstance(region, dict):
            raise ValidationError(f"{context}: expected an object")
        slr = _string(region.get("slr"), f"{context}.slr")
        clock_region = _string(
            region.get("clock_region"), f"{context}.clock_region"
        )
        if slr not in slrs:
            raise ValidationError(f"{context}.slr: unknown SLR {slr!r}")
        if clock_to_slr.get(clock_region) != slr:
            raise ValidationError(
                f"{context}: clock region/SLR relationship is invalid"
            )
        io_bank = region.get("io_bank")
        if io_bank is not None and io_bank not in bank_names:
            raise ValidationError(f"{context}.io_bank: unknown I/O bank")
        counts[slr] += 1
    return {
        "status": "pass",
        "part": architecture.part,
        "sites": len(architecture.value["sites"]),
        "slrs": len(slrs),
        "clock_regions": len(clock_to_slr),
        "sites_per_slr": dict(sorted(counts.items())),
        "io_banks": len(raw_banks),
        "package_pins": len(package_pins),
        "qualification": model.get("qualification"),
    }


def run_physical_region_merge(
    *,
    architecture_path: Path,
    sidecar_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    architecture = ArchitectureDB.load(architecture_path)
    sidecar = read_json(sidecar_path)
    bound_hash = sidecar.get("source", {}).get("architecture_sha256")
    if bound_hash is not None and bound_hash != _sha256(architecture_path):
        raise ValidationError(
            "physical-region sidecar was generated for a different "
            "ArchitectureDB artifact"
        )
    merged = merge_physical_regions(
        architecture, sidecar, sidecar_path=sidecar_path
    )
    write_json(output_path, merged.to_dict())
    return validate_fpga_interchange_architecture_regions(
        ArchitectureDB.load(output_path)
    )


def run_physical_region_rebind(
    *,
    old_architecture_path: Path,
    new_architecture_path: Path,
    sidecar_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    """Rebind a sidecar only across physically identical ArchitectureDBs."""
    old_architecture = ArchitectureDB.load(old_architecture_path)
    new_architecture = ArchitectureDB.load(new_architecture_path)
    sidecar = read_json(sidecar_path)
    old_sha256 = _sha256(old_architecture_path)
    new_sha256 = _sha256(new_architecture_path)
    bound_sha256 = sidecar.get("source", {}).get("architecture_sha256")
    if bound_sha256 != old_sha256:
        raise ValidationError(
            "physical-region sidecar is not bound to the declared old "
            "ArchitectureDB artifact"
        )
    validate_physical_region_sidecar(old_architecture, sidecar)
    mismatches = [
        field
        for field in PHYSICAL_REGION_BINDING_FIELDS
        if _physical_region_binding_value(old_architecture, field)
        != _physical_region_binding_value(new_architecture, field)
    ]
    if mismatches:
        raise ValidationError(
            "physical-region sidecar cannot be rebound because physical "
            f"ArchitectureDB fields differ: {', '.join(mismatches)}"
        )

    rebound = copy.deepcopy(sidecar)
    rebound["source"]["architecture_sha256"] = new_sha256
    rebound["source"]["architecture_rebind"] = {
        "qualification": "physical-inventory-identical-v1",
        "from_sha256": old_sha256,
        "to_sha256": new_sha256,
        "matched_fields": list(PHYSICAL_REGION_BINDING_FIELDS),
    }
    validate_physical_region_sidecar(new_architecture, rebound)
    write_json(output_path, rebound)
    return {
        "status": "pass",
        "schema": PHYSICAL_REGION_SIDECAR_SCHEMA,
        "old_architecture_sha256": old_sha256,
        "new_architecture_sha256": new_sha256,
        "matched_fields": list(PHYSICAL_REGION_BINDING_FIELDS),
        "output_sha256": _sha256(output_path),
    }
