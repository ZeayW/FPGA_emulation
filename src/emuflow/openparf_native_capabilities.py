"""Fail-closed capability probe for the pinned OpenPARF Phase 7 route.

This module does not select a placer and does not alter the production flow.
It records what the checked-in OpenPARF implementation actually contains and
which additional adapter work is required for an UltraScale+ mapped design.
The probe deliberately reads the pinned source instead of treating a README or
an importable Python package as evidence of legalization support.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placer_capability import (
    XILINX_PLACER_CAPABILITY_SCHEMA,
    XILINX_PLACER_CAPABILITY_STATUSES,
    validate_xilinx_placer_capability_report,
)


OPENPARF_NATIVE_CAPABILITY_SCHEMA = XILINX_PLACER_CAPABILITY_SCHEMA
CAPABILITY_STATUSES = XILINX_PLACER_CAPABILITY_STATUSES

_KNOWN_PLACED_PRIMITIVES = {
    *(f"LUT{width}" for width in range(1, 7)),
    "LUT6_2",
    "FDCE",
    "FDPE",
    "FDRE",
    "FDSE",
    "MUXF7",
    "MUXF8",
    "MUXF9",
    "CARRY8",
    "DSP48E2",
    "RAMB18E2",
    "RAMB36E2",
    "URAM288",
}
_UNPLACED_CONSTANTS = {"GND", "VCC"}


def _source_check(path: Path, markers: Sequence[str]) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "unverified", "path": str(path), "markers": []}
    present = [marker for marker in markers if marker in text]
    return {
        "status": "native_supported" if len(present) == len(markers) else "core_missing",
        "path": str(path),
        "markers": present,
    }


def audit_pinned_openparf_source(source_root: Path) -> Dict[str, Any]:
    """Inspect implementation symbols used by the proposed native route."""

    root = source_root.resolve()
    checks = {
        "generic_cluster_dispatch": _source_check(
            root / "openparf/placement/placer.py",
            (
                "if self.params.generic_cluster_placement_flag:",
                "self.op_cls.ssr_legalize_op(pos)",
                "if self.params.detailed_place_flag:",
                "self.op_cls.ism_dp_op(",
            ),
        ),
        "operator_selection": _source_check(
            root / "openparf/placement/op_collections.py",
            (
                "if params.generic_cluster_placement_flag:",
                "self.direct_lg_op = None",
                "self.ism_dp_op = None",
                "mcf_lg.MinCostFlowLegalizer",
            ),
        ),
        "single_site_mcf": _source_check(
            root / "openparf/ops/mcf_lg/mcf_lg.py",
            (
                "is_single_site_single_resource_type",
                "add_sssir_instances",
                "self.legalizer.forward",
            ),
        ),
        "atomic_lut_ff_legalizer": _source_check(
            root / "openparf/ops/direct_lg/direct_lg.py",
            ("class DirectLegalize",),
        ),
        "atomic_detailed_placer": _source_check(
            root / "openparf/ops/ism_dp/ism_dp.py",
            ("class ISMDetailedPlace",),
        ),
        "chain_legalizer": _source_check(
            root / "openparf/ops/chain_legalizer/chain_legalizer.py",
            ("class ChainLegalizer",),
        ),
        "legality_checker": _source_check(
            root / "openparf/ops/legality_check/legality_check.py",
            ("class LegalityCheck", "legality_check_cpp.forward"),
        ),
    }
    digest = hashlib.sha256()
    for name, check in sorted(checks.items()):
        path = Path(check["path"])
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"MISSING")
        digest.update(b"\0")
    return {
        "root": str(root),
        "revision": f"SOURCE-SHA256:{digest.hexdigest()}",
        "checks": checks,
    }


def _select_cells(mapped: Mapping[str, Any], top: Optional[str]) -> Mapping[str, Any]:
    modules = mapped.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped JSON modules are invalid")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped JSON has no top module {top!r}")
    elif len(modules) == 1:
        module = next(iter(modules.values()))
    else:
        selected = [
            module for module in modules.values()
            if isinstance(module, dict)
            and str(module.get("attributes", {}).get("top", "0")) != "0"
        ]
        if len(selected) != 1:
            raise ValidationError("mapped JSON top module is ambiguous")
        module = selected[0]
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped JSON cells are invalid")
    return cells


def _physical_coordinate(site: Mapping[str, Any]) -> tuple[int, int]:
    tile = site.get("tile")
    if isinstance(tile, Mapping):
        col, row = tile.get("grid_col"), tile.get("grid_row")
        if isinstance(col, int) and isinstance(row, int):
            return col, row
    return int(site["x"]), int(site["y"])


def _site_resource(site_type: str) -> Optional[str]:
    upper = site_type.upper()
    if upper.startswith("SLICE"):
        return "X_SLICE"
    if upper.startswith("DSP"):
        return "X_DSP"
    if upper.startswith("RAMB"):
        return "X_BRAM"
    if upper.startswith("URAM"):
        return "X_URAM"
    return None


def _source_has(source_audit: Mapping[str, Any], check: str) -> bool:
    return (
        source_audit.get("checks", {}).get(check, {}).get("status")
        == "native_supported"
    )


def probe_openparf_native_capabilities(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    *,
    top: Optional[str] = None,
    source_root: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a design-specific native-OpenPARF capability matrix.

    A status describes the complete EmuFlow-to-OpenPARF contract, not merely
    whether a similarly named upstream class exists.  In particular, the
    atomic LUT/FF legalizer and ISM detailed placer are not compatible with
    the current one-node-per-packed-site representation.
    """

    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    cells = _select_cells(mapped, top if top is not None else packed.get("top"))
    architecture = ArchitectureDB.load(architecture_path)
    if source_root is None:
        source_root = Path(__file__).resolve().parents[2] / "engines/openparf"
    source_audit = audit_pinned_openparf_source(source_root)

    primitive_counts = Counter(str(cell.get("type")) for cell in cells.values())
    primitive_matrix = []
    primitive_capabilities = {}
    for primitive, count in sorted(primitive_counts.items()):
        if primitive in _UNPLACED_CONSTANTS:
            status = "native_supported"
            reason = "constant pseudo-cells do not require a physical placement site"
        elif primitive in _KNOWN_PLACED_PRIMITIVES:
            status = "adapter_required"
            reason = (
                "the current adapter pre-packs this primitive into a site node; "
                "native atomic legalization/detailed placement cannot consume that node"
            )
        else:
            status = "unverified"
            reason = "the pinned Xilinx/OpenPARF adapter has no audited primitive contract"
        primitive_matrix.append({
            "primitive": primitive,
            "instances": count,
            "status": status,
            "reason": reason,
        })
        primitive_capabilities[primitive] = {
            "status": status,
            "evidence": [
                "src/emuflow/xilinx_packing.py",
                "openparf/placement/op_collections.py",
            ],
            **(
                {"adapter_validation": "missing"}
                if status == "adapter_required"
                else {}
            ),
            "reason": reason,
        }

    used_resources = set()
    for cluster in packed.get("clusters", []):
        kind = cluster.get("kind")
        if kind in {"slice", "carry"}:
            used_resources.add("X_SLICE")
            continue
        types = {item.get("cell_type") for item in cluster.get("assignments", [])}
        if types == {"DSP48E2"}:
            used_resources.add("X_DSP")
        elif types and all(str(item).startswith("RAMB") for item in types):
            used_resources.add("X_BRAM")
        elif types == {"URAM288"}:
            used_resources.add("X_URAM")

    coordinate_resources: Dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    for site in architecture.value.get("sites", []):
        resource = _site_resource(str(site.get("type", "")))
        if resource in used_resources:
            coordinate_resources[_physical_coordinate(site)][resource] += 1
    ambiguous_coordinates = [
        {
            "x": coordinate[0],
            "y": coordinate[1],
            "resources": dict(sorted(resources.items())),
        }
        for coordinate, resources in sorted(coordinate_resources.items())
        if sum(resources.values()) != 1 or len(resources) != 1
    ]
    cascades = packed.get("cascade_chains", [])
    if not isinstance(cascades, list):
        raise ValidationError("PackedSiteNetlist cascade_chains is invalid")

    generic_dispatch = _source_has(source_audit, "generic_cluster_dispatch")
    operator_selection = _source_has(source_audit, "operator_selection")
    mcf = _source_has(source_audit, "single_site_mcf")
    direct = _source_has(source_audit, "atomic_lut_ff_legalizer")
    ism = _source_has(source_audit, "atomic_detailed_placer")
    chain = _source_has(source_audit, "chain_legalizer")
    checker = _source_has(source_audit, "legality_checker")
    from .xilinx_openparf_atomic import (
        probe_xilinx_openparf_atomic_eligibility,
    )

    atomic_adapter = probe_xilinx_openparf_atomic_eligibility(
        mapped, packed, architecture,
        top=top if top is not None else packed.get("top"),
    )
    atomic_ready = (
        atomic_adapter["eligible"] and direct and ism and operator_selection
    )
    for primitive in sorted(primitive_capabilities):
        if primitive in _KNOWN_PLACED_PRIMITIVES and (
            primitive.startswith("LUT") and primitive != "LUT6_2"
            or primitive in {"FDCE", "FDPE", "FDRE", "FDSE"}
        ):
            primitive_capabilities[primitive] = {
                "status": "adapter_required",
                "evidence": [
                    "src/emuflow/xilinx_openparf_atomic.py",
                    "openparf/ops/direct_lg/direct_lg.py",
                ],
                "adapter_validation": "pass" if atomic_ready else "missing",
                "reason": (
                    "the atomic adapter preserves LUT/FF connectivity, control "
                    "sets, discrete slot occupancy, and physical BEL compatibility"
                    if atomic_ready else atomic_adapter["reason"]
                ),
            }

    def feature(name: str, status: str, reason: str, evidence: Sequence[str]) -> Dict[str, Any]:
        if status not in CAPABILITY_STATUSES:
            raise AssertionError(f"invalid capability status {status!r}")
        return {
            "feature": name,
            "status": status,
            "reason": reason,
            "evidence": list(evidence),
        }

    features = [
        feature(
            "continuous_global_placement",
            "native_supported" if generic_dispatch else "unverified",
            "the pinned nonlinear placer accepts architecture-defined area types",
            ("openparf/placement/placer.py",),
        ),
        feature(
            "single_site_resource_mcf_legalization",
            "adapter_required" if generic_dispatch and mcf else "core_missing",
            (
                "the core MCF legalizer exists, but the current dense-grid "
                "adapter deliberately makes X_SLICE multi-resource and merges "
                "coincident physical sites into one Bookshelf site"
            ),
            ("openparf/ops/mcf_lg/mcf_lg.py", "src/emuflow/xilinx_openparf.py"),
        ),
        feature(
            "atomic_lut_ff_legalization",
            "adapter_required" if direct and operator_selection else "core_missing",
            (
                "the native direct legalizer is connected through the audited "
                "atomic LUT/FF adapter for eligible fixtures"
                if atomic_ready else atomic_adapter["reason"]
            ),
            ("src/emuflow/xilinx_openparf_atomic.py", "openparf/ops/direct_lg/direct_lg.py"),
        ),
        feature(
            "packed_cluster_detailed_placement",
            "core_missing" if operator_selection else "unverified",
            "generic packed-cluster mode sets ism_dp_op to None; enabling detailed placement would call that missing operator",
            ("openparf/placement/op_collections.py", "openparf/placement/placer.py"),
        ),
        feature(
            "atomic_lut_ff_detailed_placement",
            "adapter_required" if ism else "core_missing",
            (
                "ISM output is independently aggregated and checked by the "
                "atomic placement certificate"
                if atomic_ready else atomic_adapter["reason"]
            ),
            ("src/emuflow/xilinx_openparf_atomic.py", "openparf/ops/ism_dp/ism_dp.py"),
        ),
        feature(
            "dedicated_cascade_legalization",
            "adapter_required" if chain else "core_missing",
            "chain legalization requires native chain metadata that the current packed Bookshelf adapter does not emit",
            ("openparf/ops/chain_legalizer/chain_legalizer.py",),
        ),
        feature(
            "exact_site_bel_and_site_mode_legality",
            "core_missing" if checker else "unverified",
            "the core checker validates OpenPARF resource occupancy, not UltraScale+ BEL assignments and alternate RAM site modes",
            ("openparf/ops/legality_check/legality_check.py", "src/emuflow/xilinx_placement.py"),
        ),
        feature(
            "clock_region_half_column_constraints",
            "adapter_required",
            "the core has clock-aware operators, but the current Bookshelf adapter emits no clock-region or instance-clock binding",
            ("openparf/placement/op_collections.py", "src/emuflow/xilinx_openparf.py"),
        ),
        feature(
            "multi_slr_constraints",
            "adapter_required",
            "the current adapter restricts a site window but does not construct OpenPARF SLR geometry or enable slr_aware_flag",
            ("src/emuflow/xilinx_openparf.py",),
        ),
    ]

    smoke_reasons = []
    if not (generic_dispatch and mcf):
        smoke_reasons.append("pinned generic-cluster MCF dispatch is unavailable")
    if ambiguous_coordinates:
        smoke_reasons.append("used physical tile coordinates are not one-site/one-resource")
    if cascades:
        smoke_reasons.append("independent MCF placement cannot preserve dedicated cascades")
    if any(item["status"] == "unverified" for item in primitive_matrix):
        smoke_reasons.append("mapped design contains an unaudited primitive")

    stage_capabilities = {
        "global_placement": {
            "status": "native_supported" if generic_dispatch else "unverified",
            "evidence": ["openparf/placement/placer.py"],
        },
        "packing": {
            "status": "adapter_required",
            "evidence": [
                "src/emuflow/xilinx_packing.py",
                "tests/test_xilinx_packing.py",
            ],
            "adapter_validation": "pass",
        },
        "legalization": {
            "status": "adapter_required" if atomic_ready else "core_missing",
            "evidence": [
                "openparf/ops/direct_lg/direct_lg.py",
                "src/emuflow/xilinx_openparf_atomic.py",
            ],
            **({"adapter_validation": "pass"} if atomic_ready else {}),
        },
        "detailed_placement": {
            "status": "adapter_required" if atomic_ready else "core_missing",
            "evidence": [
                "openparf/ops/ism_dp/ism_dp.py",
                "src/emuflow/xilinx_openparf_atomic.py",
            ],
            **({"adapter_validation": "pass"} if atomic_ready else {}),
        },
        "physical_export": {
            "status": "adapter_required",
            "evidence": [
                "src/emuflow/xilinx_openparf_atomic.py",
                "src/emuflow/architecture.py",
            ],
            "adapter_validation": "pass" if atomic_ready else "missing",
        },
    }
    constraint_capabilities = {
        "clock_region": {
            "status": "adapter_required",
            "evidence": ["openparf/placement/op_collections.py"],
            "adapter_validation": "missing",
        },
        "half_column": {
            "status": "adapter_required",
            "evidence": ["openparf/placement/op_collections.py"],
            "adapter_validation": "missing",
        },
        "multi_slr": {
            "status": "adapter_required",
            "evidence": ["src/emuflow/xilinx_openparf.py"],
            "adapter_validation": "missing",
        },
        "dedicated_cascade": {
            "status": "adapter_required" if chain else "core_missing",
            "evidence": ["openparf/ops/chain_legalizer/chain_legalizer.py"],
            **({"adapter_validation": "missing"} if chain else {}),
        },
        "bel_site_mode": {
            "status": "adapter_required" if atomic_ready else (
                "core_missing" if checker else "unverified"
            ),
            "evidence": [
                "src/emuflow/xilinx_openparf_atomic.py",
                "src/emuflow/architecture.py",
            ],
            **({"adapter_validation": "pass"} if atomic_ready else {}),
        },
    }

    result = {
        "schema": OPENPARF_NATIVE_CAPABILITY_SCHEMA,
        "provider": "openparf-native-ultrascaleplus-probe-v1",
        "revision": source_audit["revision"],
        "stages": stage_capabilities,
        "primitives": primitive_capabilities,
        "constraints": constraint_capabilities,
        "current_production_path": (
            "fixed-site-prepack -> OpenPARF-continuous-global-guidance -> "
            "EmuFlow-greedy-exact-legalizer"
        ),
        "source_audit": source_audit,
        "primitive_inventory": primitive_matrix,
        "provider_features": features,
        "design_hazards": {
            "dedicated_cascade_chains": len(cascades),
            "ambiguous_physical_coordinates": ambiguous_coordinates,
        },
        "native_packed_cluster_smoke": {
            "status": "unverified" if not smoke_reasons else "adapter_required",
            "eligible": not smoke_reasons,
            "reasons": smoke_reasons,
            "scope": (
                "single-site resource MCF core smoke only; this is not "
                "production detailed placement or UltraScale+ BEL signoff"
            ),
        },
        "native_atomic_lut_ff": atomic_adapter,
    }
    validate_xilinx_placer_capability_report(result)
    if output_path is not None:
        write_json(output_path, result, compact=True)
    return result


def require_native_packed_cluster_smoke(matrix: Mapping[str, Any]) -> None:
    """Reject unsupported probes instead of falling back to the greedy route."""

    if matrix.get("schema") != OPENPARF_NATIVE_CAPABILITY_SCHEMA:
        raise ValidationError("OpenPARF native capability matrix is invalid")
    smoke = matrix.get("native_packed_cluster_smoke")
    if not isinstance(smoke, Mapping) or smoke.get("eligible") is not True:
        reasons = smoke.get("reasons", []) if isinstance(smoke, Mapping) else []
        detail = "; ".join(str(reason) for reason in reasons) or "unknown capability"
        raise ValidationError(
            "OpenPARF native packed-cluster smoke is unsupported: " + detail
        )
