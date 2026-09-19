from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .errors import ImportError, ValidationError
from .io import read_json


ARCHDB_SCHEMA = "emuflow.archdb/v1"
_SITE_NAME_RE = re.compile(r"^(?P<type>[A-Z0-9_]+)_X(?P<x>\d+)Y(?P<y>\d+)$")
_LUT_BEL_RE = re.compile(r"^[A-H]6LUT$")
_FF_BEL_RE = re.compile(r"^[A-H]FF2?$")


def compatible_cells_for_bel(bel_name: str, bel_type: str) -> List[str]:
    """Return the deliberately conservative Phase 2 cell compatibility set."""
    local_name = bel_name.rsplit("/", 1)[-1]
    upper_type = bel_type.upper()
    if _LUT_BEL_RE.fullmatch(local_name) or upper_type == "LUT6":
        return [*[f"LUT{width}" for width in range(1, 7)], "LUT6_2"]
    if _FF_BEL_RE.fullmatch(local_name) or upper_type in {"FDRE", "FF"}:
        return ["FDCE", "FDPE", "FDRE", "FDSE"]
    if local_name == "CARRY8" or upper_type == "CARRY8":
        return ["CARRY8"]
    if upper_type == "DSP48E2" or local_name.startswith("DSP48E2"):
        return ["DSP48E2"]
    if upper_type == "RAMB36E2" or local_name.startswith("RAMB36"):
        return ["RAMB36E2"]
    return []


class ArchitectureDB:
    def __init__(self, value: Mapping[str, Any]):
        self.value = dict(value)
        self.validate()
        self._site_templates = dict(self.value.get("site_templates", {}))
        self._sites_by_name = {
            site["name"]: site for site in self.value["sites"]
        }
        self._sites_by_xy = {
            (site["x"], site["y"]): site for site in self.value["sites"]
        }

    @classmethod
    def load(cls, path: Path) -> "ArchitectureDB":
        return cls(read_json(path))

    @classmethod
    def from_vivado_tsv(cls, path: Path) -> "ArchitectureDB":
        metadata: Dict[str, str] = {}
        sites: List[Dict[str, Any]] = []
        current_site: Optional[Dict[str, Any]] = None
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                record = fields[0]
                if record == "META" and len(fields) == 3:
                    metadata[fields[1]] = fields[2]
                    continue
                if record == "SITE" and len(fields) == 5:
                    current_site = {
                        "name": fields[1],
                        "type": fields[2],
                        "x": int(fields[3]),
                        "y": int(fields[4]),
                        "bels": [],
                    }
                    sites.append(current_site)
                    continue
                if record == "BEL" and len(fields) >= 4 and current_site is not None:
                    bel_name = fields[1].rsplit("/", 1)[-1]
                    bel_type = fields[2]
                    cells = compatible_cells_for_bel(bel_name, bel_type)
                    if cells:
                        current_site["bels"].append(
                            {
                                "name": bel_name,
                                "type": bel_type,
                                "z": int(fields[3]),
                                "compatible_cells": cells,
                            }
                        )
                    continue
                raise ImportError(
                    f"{path}:{line_number}: malformed Vivado architecture "
                    f"record {line!r}"
                )

        sites = [site for site in sites if site["bels"]]
        if not sites:
            raise ImportError(f"{path}: no supported placement sites were found")
        part = metadata.get("part")
        if not part:
            raise ImportError(f"{path}: missing META part record")
        return cls(
            {
                "schema": ARCHDB_SCHEMA,
                "part": part,
                "source": {
                    "format": "vivado-site-bel-tsv/v1",
                    "tool": "Vivado",
                    "tool_version": metadata.get("vivado_version", "unknown"),
                },
                "policy": {
                    "name": "ultrascale-slice-v2",
                    "description": (
                        "Use 6LUT plus both primary and FF2 register BELs; "
                        "exclude paired 5LUT packing."
                    ),
                },
                "sites": sites,
            }
        )

    def validate(self) -> None:
        value = self.value
        if value.get("schema") != ARCHDB_SCHEMA:
            raise ValidationError(
                f"arch.schema: expected {ARCHDB_SCHEMA!r}, "
                f"got {value.get('schema')!r}"
            )
        if not isinstance(value.get("part"), str) or not value["part"]:
            raise ValidationError("arch.part: expected a non-empty string")
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValidationError("arch.source: expected an object")
        if not isinstance(source.get("format"), str) or not source["format"]:
            raise ValidationError("arch.source.format: expected a non-empty string")
        policy = value.get("policy")
        if not isinstance(policy, dict):
            raise ValidationError("arch.policy: expected an object")
        if not isinstance(policy.get("name"), str) or not policy["name"]:
            raise ValidationError("arch.policy.name: expected a non-empty string")
        templates = value.get("site_templates", {})
        if not isinstance(templates, dict):
            raise ValidationError("arch.site_templates: expected an object")
        for template_name, template in templates.items():
            context = f"arch.site_templates[{template_name!r}]"
            if not isinstance(template_name, str) or not template_name:
                raise ValidationError(
                    "arch.site_templates: expected non-empty string keys"
                )
            if not isinstance(template, dict):
                raise ValidationError(f"{context}: expected an object")
            self._validate_bels(template.get("bels"), f"{context}.bels")
            alternatives = template.get("alternative_templates", [])
            if (
                not isinstance(alternatives, list)
                or not all(
                    isinstance(alternative, str)
                    and alternative in templates
                    for alternative in alternatives
                )
                or len(alternatives) != len(set(alternatives))
            ):
                raise ValidationError(
                    f"{context}.alternative_templates: "
                    "expected unique known templates"
                )
        sites = value.get("sites")
        if not isinstance(sites, list) or not sites:
            raise ValidationError("arch.sites: expected a non-empty array")

        site_names: Set[str] = set()
        site_coordinates: Set[Tuple[int, int]] = set()
        for site_index, site in enumerate(sites):
            context = f"arch.sites[{site_index}]"
            if not isinstance(site, dict):
                raise ValidationError(f"{context}: expected an object")
            for key in ("name", "type"):
                if not isinstance(site.get(key), str) or not site[key]:
                    raise ValidationError(
                        f"{context}.{key}: expected a non-empty string"
                    )
            if site["name"] in site_names:
                raise ValidationError(
                    f"{context}.name: duplicate site {site['name']!r}"
                )
            site_names.add(site["name"])
            for key in ("x", "y"):
                coordinate = site.get(key)
                if (
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, int)
                    or coordinate < 0
                ):
                    raise ValidationError(
                        f"{context}.{key}: expected a non-negative integer"
                    )
            xy = (site["x"], site["y"])
            if xy in site_coordinates:
                raise ValidationError(
                    f"{context}: duplicate placer coordinate {xy}"
                )
            site_coordinates.add(xy)
            if "bels" in site:
                self._validate_bels(site.get("bels"), f"{context}.bels")
            else:
                template_name = site.get("template")
                if (
                    not isinstance(template_name, str)
                    or template_name not in templates
                ):
                    raise ValidationError(
                        f"{context}.template: expected a known site template"
                    )

    @staticmethod
    def _validate_bels(bels: Any, context: str) -> None:
        if not isinstance(bels, list) or not bels:
            raise ValidationError(f"{context}: expected a non-empty array")
        bel_names: Set[str] = set()
        bel_slots: Set[Tuple[int, str]] = set()
        for bel_index, bel in enumerate(bels):
            bel_context = f"{context}[{bel_index}]"
            if not isinstance(bel, dict):
                raise ValidationError(f"{bel_context}: expected an object")
            for key in ("name", "type"):
                if not isinstance(bel.get(key), str) or not bel[key]:
                    raise ValidationError(
                        f"{bel_context}.{key}: expected a non-empty string"
                    )
            if bel["name"] in bel_names:
                raise ValidationError(
                    f"{bel_context}.name: duplicate BEL {bel['name']!r}"
                )
            bel_names.add(bel["name"])
            z = bel.get("z")
            if isinstance(z, bool) or not isinstance(z, int) or z < 0:
                raise ValidationError(
                    f"{bel_context}.z: expected a non-negative integer"
                )
            compatible = bel.get("compatible_cells")
            if (
                not isinstance(compatible, list)
                or not compatible
                or not all(isinstance(cell, str) and cell for cell in compatible)
            ):
                raise ValidationError(
                    f"{bel_context}.compatible_cells: expected non-empty strings"
                )
            for cell_type in compatible:
                slot = (z, cell_type)
                if slot in bel_slots:
                    raise ValidationError(
                        f"{bel_context}: z={z} is ambiguous for {cell_type}"
                    )
                bel_slots.add(slot)

    @property
    def part(self) -> str:
        return self.value["part"]

    @property
    def sites(self) -> List[Dict[str, Any]]:
        return [
            self._materialize_site(site) for site in self.value["sites"]
        ]

    def _materialize_site(
        self, site: Optional[Mapping[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if site is None:
            return None
        if "bels" in site:
            return dict(site)
        result = dict(site)
        template_name = site["template"]
        template = self._site_templates[template_name]
        modes = [template_name, *template.get("alternative_templates", [])]
        result["bels"] = [
            {**bel, "placement_mode": mode}
            for mode in modes
            for bel in self._site_templates[mode]["bels"]
        ]
        return result

    def site_at(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        return self._materialize_site(self._sites_by_xy.get((x, y)))

    def site_named(self, name: str) -> Optional[Dict[str, Any]]:
        return self._materialize_site(self._sites_by_name.get(name))

    def legal_bel(
        self, site: Mapping[str, Any], cell_type: str, z: int
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            bel
            for bel in site["bels"]
            if bel["z"] == z and cell_type in bel["compatible_cells"]
        ]
        return candidates[0] if len(candidates) == 1 else None

    def compatible_bels(
        self, site: Mapping[str, Any], cell_type: str
    ) -> Iterable[Dict[str, Any]]:
        return (
            bel
            for bel in site["bels"]
            if cell_type in bel["compatible_cells"]
        )

    def summary(self) -> Dict[str, Any]:
        site_types = Counter(site["type"] for site in self.value["sites"])
        cell_slots: Counter[str] = Counter()
        compact_site_counts: Counter[str] = Counter()
        for site in self.value["sites"]:
            if "bels" not in site:
                compact_site_counts[site["template"]] += 1
                continue
            for bel in site["bels"]:
                for cell_type in bel["compatible_cells"]:
                    cell_slots[cell_type] += 1
        for template_name, site_count in compact_site_counts.items():
            template = self._site_templates[template_name]
            modes = [
                template_name,
                *template.get("alternative_templates", []),
            ]
            for mode in modes:
                for bel in self._site_templates[mode]["bels"]:
                    for cell_type in bel["compatible_cells"]:
                        cell_slots[cell_type] += site_count
        return {
            "schema": ARCHDB_SCHEMA,
            "part": self.part,
            "policy": self.value["policy"]["name"],
            "sites": len(self.value["sites"]),
            "site_types": dict(sorted(site_types.items())),
            "cell_slots": dict(sorted(cell_slots.items())),
        }

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.value)
