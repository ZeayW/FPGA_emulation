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
from .xilinx_physical_macros import (
    XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA,
    validate_xilinx_physical_macro_contract,
)
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
        "mixed_atomic_sssir_dispatch": _source_check(
            root / "openparf/placement/placer.py",
            (
                "self.op_cls.ssr_legalize_op(self.data_cls.pos[0])",
                "pos_xyz = self.op_cls.direct_lg_op(pos)",
                "loc_xyz = self.op_cls.ism_dp_op(self.data_cls.inst_locs_xyz)",
            ),
        ),
        "ism_sssir_preservation": _source_check(
            root / "openparf/ops/ism_dp/src/ism_detailed_placer.hpp",
            ("case ResourceCategory::kSSSIR:",),
        ),
        "mcf_locks_sssir_solution": _source_check(
            root / "openparf/ops/mcf_lg/mcf_lg.py",
            (
                "pos.data.copy_(res)",
                "self.data_cls.inst_lock_mask[self.inst_ids_groups[i]] = 1",
                "self.data_cls.lock_area_types(self.data_cls.ssr_area_types)",
            ),
        ),
        "direct_lg_preserves_non_slice_xy": _source_check(
            root / "openparf/ops/direct_lg/src/direct_lg_kernel.h",
            (
                "if (db.isInstLUT(i) || db.isInstFF(i))",
                "pos[i * 3]     = init_pos[i * 2]",
                "pos[i * 3 + 1] = init_pos[i * 2 + 1]",
            ),
        ),
        "typed_hardblock_legalizer": _source_check(
            root / "openparf/ops/typed_hardblock_legalizer/typed_hardblock_legalizer.py",
            (
                "openparf.physical-macro-groups/v1",
                'SUPPORTED_RESOURCES = {"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}',
                'site["claims"]',
                "occupied.update(unique_claims)",
            ),
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


def audit_pinned_openparf_carry_path(source_root: Path) -> Dict[str, Any]:
    """Audit the actual native carry input/legalization/DP implementation.

    Source markers are evidence only for the implementation shape.  They do
    not replace an executed placement plus independent device-legality check.
    """

    root = source_root.resolve()
    paths = {
        "parser": root / "openparf/io/bookshelf/bookshelf_parser.yy",
        "shape_db": root / "openparf/database/database.cpp",
        "chain_info": (
            root / "openparf/custom_data/chain_info/src/chain_info.cpp"
        ),
        "chain_legalizer": (
            root / "openparf/ops/chain_legalizer/src/chain_legalizer.cpp"
        ),
        "placer": root / "openparf/placement/placer.py",
    }
    texts: Dict[str, Optional[str]] = {}
    for name, path in paths.items():
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except OSError:
            texts[name] = None

    def evidence(
        name: str,
        *,
        status: str,
        reason: str,
        markers: Sequence[str],
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "path": str(paths[name]),
            "markers": [
                marker for marker in markers
                if texts[name] is not None and marker in texts[name]
            ],
        }

    missing = [name for name, text in texts.items() if text is None]
    if missing:
        checks = {
            name: evidence(
                name,
                status="unverified",
                reason="pinned OpenPARF source file is unavailable",
                markers=(),
            )
            for name in paths
        }
    else:
        shape_consumers = []
        for directory_name in ("placement", "ops"):
            directory = root / "openparf" / directory_name
            for path in sorted(directory.rglob("*")):
                if path.suffix not in {".py", ".cpp", ".h", ".hpp"}:
                    continue
                try:
                    candidate = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "shapeConstr" in candidate or "shape_constr" in candidate:
                    shape_consumers.append(str(path.relative_to(root)))
        output_cas_correct = (
            "KWD_OUTPUT KWD_CAS ENDL    { driver.addCellOutputCasPinCbk"
            in texts["parser"]
        )
        carry8_extractor = all(marker in texts["chain_info"] for marker in (
            'model.name() == "CARRY8"',
            "associated_luts_per_unit = is_carry8 ? 8 : 4",
            'name.rfind("DI", 0)',
            "adjacent_lut_id == ordinal_lut_ids",
        ))
        carry8_legalizer = all(
            marker in texts["chain_legalizer"] for marker in (
                "luts_per_unit == 4 || luts_per_unit == 8",
                "luts_per_unit == 8 ? 1.0 : 0.5",
                "z + j * 2",
                "site.bbox().yh()",
            )
        )
        shape_only_loaded = (
            "addShapeCbk" in texts["shape_db"]
            and "addShapeNodeCbk" in texts["shape_db"]
            and not shape_consumers
        )
        dp_mask_is_carry_gated = all(marker in texts["placer"] for marker in (
            "self.params.carry_chain_legalization_flag",
            "self.data_cls.chain_cla_ids.bs",
            "self.data_cls.chain_lut_ids.bs",
            "fixed_mask[inst_ids] = 1",
            "self.op_cls.ism_dp_op.fixed_mask = fixed_mask",
        ))
        carry_seed_is_gp_backed = (
            all(marker in texts["placer"] for marker in (
                "Seed native macro legalization from the current GP solution",
                "pos_xyz = self.data_cls.inst_locs_xyz.to(",
                "pos[movable_range[0] : movable_range[1]]",
            ))
            and "assert self.data_cls.io_pos_xyz is not None"
            not in texts["placer"]
        )
        checks = {
            "parser": evidence(
                "parser",
                status=(
                    "native_supported" if output_cas_correct else "core_missing"
                ),
                reason=(
                    "Bookshelf INPUT/OUTPUT CAS pins dispatch to direction-correct callbacks"
                    if output_cas_correct else
                    "Bookshelf cascade output semantics are incorrect or unproven"
                ),
                markers=(
                    "KWD_INPUT KWD_CAS",
                    "KWD_OUTPUT KWD_CAS",
                    "driver.addCellInputCasPinCbk",
                    "driver.addCellOutputCasPinCbk",
                ),
            ),
            "shape_db": evidence(
                "shape_db",
                status="adapter_required" if shape_only_loaded else "unverified",
                reason=(
                    "Bookshelf Carry-chain shape records are loaded into the "
                    "database but are not used by the explicit typed CARRY8 "
                    "chain route; native extraction uses cascade and DI/S connectivity"
                    if shape_only_loaded else
                    "shape-constraint ingestion was not proven"
                ),
                markers=("addShapeCbk", "addShapeNodeCbk"),
            ),
            "chain_info": evidence(
                "chain_info",
                status="native_supported" if carry8_extractor else "core_missing",
                reason=(
                    "native extraction selects eight ordered LUT6_2 members "
                    "for CARRY8 and proves each DI/S pair has one common driver"
                    if carry8_extractor else
                    "native CARRY8 eight-LUT member extraction is missing"
                ),
                markers=(
                    'model.name() == "CARRY8"',
                    "associated_luts_per_unit = is_carry8 ? 8 : 4",
                    'name.rfind("DI", 0)',
                    "adjacent_lut_id == ordinal_lut_ids",
                ),
            ),
            "chain_legalizer": evidence(
                "chain_legalizer",
                status="native_supported" if carry8_legalizer else "core_missing",
                reason=(
                    "native legalization infers four- or eight-LUT units and "
                    "places each eight-LUT CARRY8 unit as one full slice"
                    if carry8_legalizer else
                    "native full-slice CARRY8 legalization is missing"
                ),
                markers=(
                    "luts_per_unit == 4 || luts_per_unit == 8",
                    "luts_per_unit == 8 ? 1.0 : 0.5",
                    "z + j * 2",
                    "site.bbox().yh()",
                ),
            ),
            "placer": evidence(
                "placer",
                status=(
                    "native_supported"
                    if dp_mask_is_carry_gated and carry_seed_is_gp_backed
                    else "core_missing"
                ),
                reason=(
                    "carry legalization is seeded from GP and ISM fixes both "
                    "carry primitives and associated LUTs whenever carry-chain "
                    "legalization is active"
                    if dp_mask_is_carry_gated and carry_seed_is_gp_backed else
                    "carry preservation through detailed placement is incorrectly gated or unproven"
                ),
                markers=(
                    "if self.params.carry_chain_legalization_flag:",
                    "self.data_cls.chain_cla_ids.bs",
                    "self.data_cls.chain_lut_ids.bs",
                    "pos_xyz = self.data_cls.inst_locs_xyz.to(",
                    "fixed_mask[inst_ids] = 1",
                    "self.op_cls.ism_dp_op.fixed_mask = fixed_mask",
                ),
            ),
        }
        checks["shape_db"]["placement_or_operator_consumers"] = shape_consumers

    digest = hashlib.sha256()
    for name, path in sorted(paths.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"MISSING")
        digest.update(b"\0")
    return {
        "root": str(root),
        "revision": f"SOURCE-SHA256:{digest.hexdigest()}",
        "status": (
            "unverified" if missing else
            "core_missing" if any(
                checks[name]["status"] == "core_missing"
                for name in ("parser", "chain_info", "chain_legalizer", "placer")
            ) else "native_supported"
        ),
        "checks": checks,
    }


def probe_xilinx_openparf_carry_native_support(
    mapped_path: Path,
    macro_contract_path: Path,
    *,
    top: Optional[str] = None,
    source_root: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Report source capability while requiring runtime CARRY8 qualification.

    This is a qualification gate, not a placer.  It never emits Bookshelf
    input, calls OpenPARF, chooses sites, or preplaces macro members.
    """

    validate_xilinx_physical_macro_contract(
        mapped_path, macro_contract_path, top=top
    )
    contract = read_json(macro_contract_path)
    if contract.get("schema") != XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA:
        raise ValidationError("physical macro contract schema is invalid")
    carry_macros = [
        item for item in contract.get("site_macros", [])
        if item.get("kind") == "carry8-lut6_2"
    ]
    carry_chains = [
        item for item in contract.get("cascade_chains", [])
        if item.get("cell_type") == "CARRY8"
    ]
    if not carry_macros:
        raise ValidationError("carry qualification requires at least one CARRY8 macro")
    for macro in carry_macros:
        counts = Counter(member.get("cell_type") for member in macro["members"])
        if counts != Counter({"CARRY8": 1, "LUT6_2": 8}):
            raise ValidationError("CARRY8 qualification macro membership is invalid")
        if len(macro.get("connections", [])) != 16:
            raise ValidationError("CARRY8 qualification macro connectivity is invalid")

    if source_root is None:
        source_root = Path(__file__).resolve().parents[2] / "engines/openparf"
    source_audit = audit_pinned_openparf_carry_path(source_root)
    source_status = source_audit["status"]
    qualification_status = (
        "unverified" if source_status == "native_supported" else source_status
    )
    evidence = [
        str(Path(check["path"]).relative_to(Path(source_audit["root"])))
        for check in source_audit["checks"].values()
    ]

    def entry(entry_status: str, *paths: str) -> Dict[str, Any]:
        return {"status": entry_status, "evidence": list(paths)}

    report = {
        "schema": XILINX_PLACER_CAPABILITY_SCHEMA,
        "provider": "openparf-native-carry8-lut6_2-probe-v1",
        "revision": source_audit["revision"],
        "stages": {
            "global_placement": entry(
                qualification_status,
                "openparf/placement/placer.py",
                "openparf/custom_data/chain_info/src/chain_info.cpp",
            ),
            "packing": {
                "status": "adapter_required",
                "evidence": [
                    "src/emuflow/xilinx_physical_macros.py",
                    "tests/test_xilinx_physical_macros.py",
                ],
                "adapter_validation": "pass",
            },
            "legalization": entry(
                qualification_status,
                "openparf/ops/chain_legalizer/src/chain_legalizer.cpp",
            ),
            "detailed_placement": entry(
                qualification_status, "openparf/placement/placer.py"
            ),
            "physical_export": {
                "status": "adapter_required",
                "evidence": ["src/emuflow/xilinx_openparf_bridge.py"],
                "adapter_validation": "missing",
            },
        },
        "primitives": {
            "CARRY8": entry(qualification_status, *evidence),
            "LUT6_2": entry(qualification_status, *evidence),
        },
        "constraints": {
            "indivisible_carry8_lut6_2_macro": entry(
                qualification_status, *evidence
            ),
            "ordered_carry8_chain": entry(qualification_status, *evidence),
        },
        "qualification": {
            "status": qualification_status,
            "runtime_launched": False,
            "fallback": "forbidden",
            "preplacement": "forbidden",
            "reason": (
                "the pinned native core cannot represent or legalize the "
                "required CARRY8 plus eight LUT6_2 macro semantics"
                if source_status == "core_missing" else
                "source support is present, but compiled native placement and "
                "independent RapidWright device-legality validation have not run"
                if source_status == "native_supported" else
                "the pinned carry implementation could not be audited"
            ),
        },
        "minimum_reproduction": {
            "carry8_units": len(carry_macros),
            "lut6_2_adapters": 8 * len(carry_macros),
            "carry_chain_lengths": [
                len(chain["members"]) for chain in carry_chains
            ],
            "associated_luts_per_unit": 8,
            "required_lut_outputs": ["O5", "O6"],
            "native_lut_interface": "DI[0:7]/S[0:7] paired LUT6_2 drivers",
        },
        "remaining_qualification_steps": [
            {
                "id": "compiled-openparf-carry8-run",
                "component": "src/emuflow/xilinx_openparf_carry8.py",
                "requirement": (
                    "build the modified core and execute unplaced GP through "
                    "native chain legalization and detailed placement"
                ),
            },
            {
                "id": "rapidwright-native-legality",
                "component": "src/emuflow/xilinx_openparf_carry8.py",
                "requirement": (
                    "independently prove same-site CARRY8/LUT6_2 roles and "
                    "CARRY_NEXT adjacency against certified device constraints"
                ),
            },
            {
                "id": "rapidwright-route-bridge",
                "component": "src/emuflow/xilinx_openparf_bridge.py",
                "requirement": (
                    "preserve the qualified macro placement through physical "
                    "netlist export and route it without greedy fallback"
                ),
            },
            {
                "id": "opensta-timing-gate",
                "component": "src/emuflow/opensta_global_timing.py",
                "requirement": (
                    "run the independent routed endpoint and global timing gate"
                ),
            },
        ],
        "source_audit": source_audit,
    }
    validate_xilinx_placer_capability_report(report)
    if output_path is not None:
        write_json(output_path, report)
    return report


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
    carry_source_audit = audit_pinned_openparf_carry_path(source_root)
    carry_source_ready = carry_source_audit["status"] == "native_supported"

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
    mixed_dispatch = _source_has(source_audit, "mixed_atomic_sssir_dispatch")
    ism_sssir = _source_has(source_audit, "ism_sssir_preservation")
    mcf_lock = _source_has(source_audit, "mcf_locks_sssir_solution")
    direct_preserve = _source_has(source_audit, "direct_lg_preserves_non_slice_xy")
    typed_hardblock = _source_has(source_audit, "typed_hardblock_legalizer")
    from .xilinx_openparf_atomic import (
        probe_xilinx_openparf_atomic_eligibility,
    )

    atomic_adapter = probe_xilinx_openparf_atomic_eligibility(
        mapped, packed, architecture,
        top=top if top is not None else packed.get("top"),
        allow_typed_hardblocks=typed_hardblock,
    )
    atomic_ready = (
        atomic_adapter["eligible"] and direct and ism and operator_selection
        and mixed_dispatch and ism_sssir and mcf_lock and direct_preserve
    )
    atomic_primitives = {
        *(f"LUT{width}" for width in range(1, 7)),
        "FDCE", "FDPE", "FDRE", "FDSE",
        "DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288",
    }
    for primitive in sorted(primitive_capabilities):
        if primitive in atomic_primitives:
            primitive_capabilities[primitive] = {
                "status": "adapter_required",
                "evidence": [
                    "src/emuflow/xilinx_openparf_atomic.py",
                    (
                        "openparf/ops/direct_lg/direct_lg.py"
                        if primitive not in {
                            "DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"
                        }
                        else "openparf/ops/mcf_lg/mcf_lg.py"
                    ),
                ],
                "adapter_validation": "pass" if atomic_ready else "missing",
                "reason": (
                    "the atomic adapter preserves connectivity, LUT/FF control "
                    "sets, discrete resource occupancy, and physical BEL compatibility"
                    if atomic_ready else atomic_adapter["reason"]
                ),
            }
    for primitive in ("CARRY8", "LUT6_2"):
        if primitive in primitive_capabilities:
            primitive_capabilities[primitive] = {
                "status": (
                    "adapter_required" if carry_source_ready else
                    carry_source_audit["status"]
                ),
                "evidence": [
                    "openparf/custom_data/chain_info/src/chain_info.cpp",
                    "openparf/ops/chain_legalizer/src/chain_legalizer.cpp",
                    "src/emuflow/xilinx_openparf_carry8.py",
                ],
                **(
                    {"adapter_validation": "missing"}
                    if carry_source_ready else {}
                ),
                "reason": (
                    "the native core has typed CARRY8 extraction and full-slice "
                    "legalization, but the explicit adapter has not completed "
                    "compiled placement and RapidWright legality qualification"
                    if carry_source_ready else
                    "native CARRY8/LUT6_2 source support is missing or unverified"
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
            "mixed_atomic_sssir_native_flow",
            (
                "native_supported"
                if mixed_dispatch and ism_sssir and mcf_lock and direct_preserve
                else "core_missing"
            ),
            (
                "global placement invokes and locks SSSIR MCF; direct LUT/FF "
                "legalization preserves non-slice coordinates, and ISM "
                "explicitly handles SSSIR categories"
            ),
            (
                "openparf/placement/placer.py",
                "openparf/ops/mcf_lg/mcf_lg.py",
                "openparf/ops/direct_lg/src/direct_lg_kernel.h",
                "openparf/ops/ism_dp/src/ism_detailed_placer.hpp",
            ),
        ),
        feature(
            "dedicated_cascade_legalization",
            "adapter_required" if carry_source_ready else (
                "core_missing" if chain else "unverified"
            ),
            (
                "the source core supports full-slice CARRY8 chains, but this "
                "generic probe cannot substitute for an executed adapter and "
                "RapidWright legality qualification"
                if carry_source_ready else
                "native CARRY8/LUT6_2 cascade legalization is missing or unverified"
            ),
            (
                "openparf/custom_data/chain_info/src/chain_info.cpp",
                "openparf/ops/chain_legalizer/src/chain_legalizer.cpp",
            ),
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
            "status": "adapter_required" if carry_source_ready else (
                "core_missing" if chain else "unverified"
            ),
            "evidence": [
                "openparf/custom_data/chain_info/src/chain_info.cpp",
                "openparf/ops/chain_legalizer/src/chain_legalizer.cpp",
                "src/emuflow/xilinx_openparf_carry8.py",
            ],
            **(
                {"adapter_validation": "missing"}
                if carry_source_ready else {}
            ),
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
        "carry_source_audit": carry_source_audit,
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
