"""Fail-closed audit and adapter boundary for public AMF-Placer.

This module intentionally does not invoke AMF-Placer or register a Phase 7
provider. It records what the pinned public *basic* release actually exposes
through EmuFlow's common Xilinx-placer capability contract. Paper claims for
the separately distributed advanced release are not public-code capabilities.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .architecture import ARCHDB_SCHEMA
from .errors import ValidationError
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_CONSTRAINTS_SCHEMA, XILINX_PLACEMENT_SCHEMA
from .xilinx_placer_capability import (
    XILINX_PLACER_CAPABILITY_SCHEMA,
    XILINX_PLACER_CAPABILITY_STATUSES,
    validate_xilinx_placer_capability_report,
)
from .xilinx_primitives import (
    DEFAULT_XILINX_PRIMITIVE_LIBRARY,
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    load_xilinx_primitive_library,
)


AMF_PLACER_ADAPTER_CONTRACT_SCHEMA = "emuflow.amf-placer-adapter-contract/v1"
AMF_PUBLIC_PROVIDER_ID = "amf-placer-public-basic-2.0"
AMF_PUBLIC_REPOSITORY = "https://github.com/zslwyuan/AMF-Placer"
AMF_PUBLIC_AUDITED_REVISION = "70d98288153046ea4fd07190e748b6530e3042f5"
AMF_PUBLIC_LICENSE = "Apache-2.0"

# These are public DesignCellType values with corresponding placement or
# packing paths. This does not claim XCVU19P or multi-SLR qualification.
_NATIVE_PRIMITIVES = frozenset(
    {
        "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "LUT6_2",
        "FDCE", "FDPE", "FDRE", "FDSE", "CARRY8", "DSP48E2",
        "MUXF7", "MUXF8", "RAMB18E2", "RAMB36E2",
    }
)

# AMF models constants as nets. A future adapter may lower these physical
# helper cells, but that transform is not implemented or validated today.
_ADAPTER_PRIMITIVES = frozenset({"GND", "VCC"})

# Neither is a public DesignCellType and no public packing/legalization core was
# found. The MUX9 macro enum alone is not native MUXF9 cell support.
_CORE_MISSING_PRIMITIVES = frozenset({"MUXF9", "URAM288"})


def _capability(
    status: str,
    evidence: Iterable[str],
    *,
    adapter_validation: Optional[str] = None,
) -> Dict[str, Any]:
    if status not in XILINX_PLACER_CAPABILITY_STATUSES:
        raise ValidationError(f"invalid AMF capability status {status!r}")
    entry: Dict[str, Any] = {"status": status, "evidence": list(evidence)}
    if status == "adapter_required":
        entry["adapter_validation"] = adapter_validation or "missing"
    elif adapter_validation is not None:
        raise ValidationError(
            "adapter_validation is valid only for adapter_required"
        )
    return entry


def classify_amf_primitive(cell_type: str) -> Dict[str, Any]:
    """Classify one EmuFlow primitive against the pinned public release."""

    if cell_type in _NATIVE_PRIMITIVES:
        return _capability(
            "native_supported",
            (
                "src/lib/HiFPlacer/designInfo/DesignInfo.h:CELLTYPESTRS",
                "src/lib/HiFPlacer/placement/packing",
            ),
        )
    if cell_type in _ADAPTER_PRIMITIVES:
        return _capability(
            "adapter_required",
            (
                "public AMF represents constants as nets; EmuFlow physical "
                "constant cells need validated lossless lowering",
            ),
        )
    if cell_type in _CORE_MISSING_PRIMITIVES:
        return _capability(
            "core_missing",
            (
                "pinned DesignInfo.h lacks this DesignCellType and the public "
                "packing/legalization core has no implementation for it",
            ),
        )
    return _capability(
        "unverified",
        ("primitive is outside the audited EmuFlow-to-AMF matrix",),
    )


def _source_audit(source_root: Optional[Path]) -> Dict[str, Any]:
    """Optionally prove that a local source tree has the audit markers."""

    if source_root is None:
        return {"status": "not_run", "reason": "no source tree was supplied"}

    required = {
        "readme": Path("README.MD"),
        "design_types": Path("src/lib/HiFPlacer/designInfo/DesignInfo.h"),
        "design_import": Path("src/lib/HiFPlacer/designInfo/DesignInfo.cc"),
        "device_import": Path("src/lib/HiFPlacer/deviceInfo/DeviceInfo.cc"),
        "flow": Path("src/app/AMFPlacer/AMFPlacer.h"),
        "packing": Path(
            "src/lib/HiFPlacer/placement/packing/ParallelCLBPacker.h"
        ),
    }
    text: Dict[str, str] = {}
    for label, relative in required.items():
        path = source_root / relative
        if not path.is_file():
            raise ValidationError(
                f"AMF public-source probe is missing {relative.as_posix()!r}"
            )
        text[label] = path.read_text(encoding="utf-8", errors="strict")

    checks = {
        "public_basic_release": "basic implementation of AMF-Placer 2.0"
        in text["readme"],
        "vcu108_reference": "VCU108" in text["readme"],
        "clock_tree_todo": "clock tree synthesis" in text["readme"],
        "vivado_design_input": "vivado extracted design information file"
        in text["design_import"],
        "vivado_device_input": "vivado extracted device information file"
        in text["flow"],
        "initial_packing": "InitialPacker" in text["flow"],
        "global_placement": "GlobalPlacer" in text["flow"],
        "final_clb_packing": "ParallelCLBPacker" in text["flow"],
        "detailed_placement": "timingDrivenDetailedPlacement"
        in text["packing"],
        "native_primitive_types": all(
            f'"{cell_type}"' in text["design_types"]
            for cell_type in _NATIVE_PRIMITIVES
        ),
        "no_muxf9_core_type": '"MUXF9"' not in text["design_types"],
        "no_uram288_core_type": '"URAM288"' not in text["design_types"],
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValidationError(
            "AMF public-source probe disagrees with the pinned audit: "
            + ", ".join(failed)
        )
    return {
        "status": "pass",
        "files": [relative.as_posix() for relative in required.values()],
        "checks": checks,
    }


def build_amf_placer_adapter_contract() -> Dict[str, Any]:
    """Return the AMF-specific conversions required by a future provider."""

    contract = {
        "schema": AMF_PLACER_ADAPTER_CONTRACT_SCHEMA,
        "provider": AMF_PUBLIC_PROVIDER_ID,
        "revision": AMF_PUBLIC_AUDITED_REVISION,
        "inputs": {
            "mapped_netlist": {
                "format": f"yosys-json/{XILINX_ULTRASCALEPLUS_OPEN_PROFILE}",
                "status": "adapter_required",
            },
            "architecture": {
                "schema": ARCHDB_SCHEMA,
                "status": "adapter_required",
            },
            "constraints": {
                "schema": XILINX_CONSTRAINTS_SCHEMA,
                "status": "adapter_required",
            },
        },
        "outputs": {
            "packed_netlist": {
                "schema": PACKED_SITE_NETLIST_SCHEMA,
                "status": "adapter_required",
            },
            "placement": {
                "schema": XILINX_PLACEMENT_SCHEMA,
                "status": "adapter_required",
            },
        },
        "required_adapters": [
            {
                "id": "emuflow-to-amf-design-v1",
                "purpose": "emit AMF cells, pins, and nets without Vivado",
                "status": "adapter_required",
            },
            {
                "id": "archdb-to-amf-device-v1",
                "purpose": "emit sites, BEL compatibility, and clock regions",
                "status": "adapter_required",
            },
            {
                "id": "amf-result-to-emuflow-v1",
                "purpose": "import exact site/BEL packing without Tcl",
                "status": "adapter_required",
            },
        ],
        "fail_closed_if": [
            "any used primitive is core_missing or unverified",
            "any cell is lost, duplicated, or renamed ambiguously",
            "the device requires unqualified multi-SLR or clock legality",
            "the result fails EmuFlow site/BEL/cascade validation",
            "runtime Vivado extraction or Tcl execution is required",
        ],
        "production_provider_ready": False,
    }
    return validate_amf_placer_adapter_contract(contract)


def validate_amf_placer_adapter_contract(
    value: Mapping[str, Any],
) -> Dict[str, Any]:
    if value.get("schema") != AMF_PLACER_ADAPTER_CONTRACT_SCHEMA:
        raise ValidationError("AMF placer adapter contract schema is invalid")
    if value.get("provider") != AMF_PUBLIC_PROVIDER_ID:
        raise ValidationError("AMF placer adapter provider is invalid")
    if value.get("revision") != AMF_PUBLIC_AUDITED_REVISION:
        raise ValidationError("AMF placer adapter revision is invalid")
    if value.get("production_provider_ready") is not False:
        raise ValidationError(
            "the public AMF candidate must remain disabled before qualification"
        )
    for group in ("inputs", "outputs"):
        entries = value.get(group)
        if not isinstance(entries, Mapping) or not entries:
            raise ValidationError(f"AMF placer adapter {group} are missing")
        for name, entry in entries.items():
            if not isinstance(name, str) or not isinstance(entry, Mapping):
                raise ValidationError(f"AMF placer adapter {group} are invalid")
            if entry.get("status") != "adapter_required":
                raise ValidationError(
                    f"AMF placer adapter {group} entry {name!r} must remain "
                    "adapter_required"
                )
    adapters = value.get("required_adapters")
    expected = {
        "emuflow-to-amf-design-v1",
        "archdb-to-amf-device-v1",
        "amf-result-to-emuflow-v1",
    }
    if not isinstance(adapters, list) or {
        item.get("id") for item in adapters if isinstance(item, Mapping)
    } != expected:
        raise ValidationError("AMF placer required adapter set is invalid")
    if any(
        not isinstance(item, Mapping)
        or item.get("status") != "adapter_required"
        for item in adapters
    ):
        raise ValidationError("AMF placer required adapter status is invalid")
    blockers = value.get("fail_closed_if")
    if not isinstance(blockers, list) or not blockers or not all(
        isinstance(item, str) and item for item in blockers
    ):
        raise ValidationError("AMF placer fail-closed conditions are invalid")
    return dict(value)


def probe_amf_placer_capabilities(
    *,
    primitive_library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
    public_source_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Audit public AMF against the checked-in Xilinx primitive inventory."""

    _, primitive_library = load_xilinx_primitive_library(primitive_library_path)
    primitives = {
        cell_type: classify_amf_primitive(cell_type)
        for cell_type in sorted(primitive_library["cells"])
    }
    stages = {
        "global_placement": _capability(
            "native_supported",
            ("src/lib/HiFPlacer/placement/globalPlacement",),
        ),
        "packing": _capability(
            "native_supported",
            ("InitialPacker and ParallelCLBPacker in the public run flow",),
        ),
        "legalization": _capability(
            "native_supported",
            ("src/lib/HiFPlacer/placement/legalization",),
        ),
        "detailed_placement": _capability(
            "native_supported",
            (
                "ParallelCLBPacker timing-driven shortest-path, swap, and "
                "LUT/FF relocation passes",
            ),
        ),
        "physical_export": _capability(
            "adapter_required",
            ("public output is a Vivado place_cell Tcl script",),
        ),
    }
    constraints = {
        "design_netlist_import": _capability(
            "adapter_required",
            ("DesignInfo.cc consumes a Vivado-extracted archive",),
        ),
        "device_import": _capability(
            "adapter_required",
            ("DeviceInfo consumes extracted sites/BELs and compatibility data",),
        ),
        "constant_net_lowering": _capability(
            "adapter_required",
            ("GND/VCC cells require a validated constant-net transform",),
        ),
        "exact_site_bel_roundtrip": _capability(
            "adapter_required",
            ("AMF Tcl output must be imported into EmuFlow schemas",),
        ),
        "xilinx_ultrascaleplus_xcvu19p": _capability(
            "unverified",
            ("public reference data is VCU108/UltraScale, not XCVU19P",),
        ),
        "multi_slr": _capability(
            "core_missing",
            ("no explicit SLR model or SLR legality contract was found",),
        ),
        "clock_legality": _capability(
            "unverified",
            (
                "clock-region heuristics exist, but CTS is a TODO and "
                "VCU108-specific constants remain",
            ),
        ),
        "vivado_free_runtime": _capability(
            "adapter_required",
            (
                "standalone AMF can consume extracted files, but EmuFlow must "
                "replace extraction and Tcl loading",
            ),
        ),
    }
    report: Dict[str, Any] = {
        "schema": XILINX_PLACER_CAPABILITY_SCHEMA,
        "provider": AMF_PUBLIC_PROVIDER_ID,
        "revision": AMF_PUBLIC_AUDITED_REVISION,
        "stages": stages,
        "primitives": primitives,
        "constraints": constraints,
        "audit": {
            "repository": AMF_PUBLIC_REPOSITORY,
            "license": AMF_PUBLIC_LICENSE,
            "release_scope": "public basic implementation",
            "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            "source": _source_audit(public_source_root),
            "primitive_counts": dict(
                sorted(
                    Counter(
                        item["status"] for item in primitives.values()
                    ).items()
                )
            ),
        },
        "adapter_contract": build_amf_placer_adapter_contract(),
        "production_provider_ready": False,
    }
    validate_xilinx_placer_capability_report(report)
    return report
