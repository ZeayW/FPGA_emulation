"""Editable-source inventory and compatibility checks for serial PHY providers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .serial_contract import (
    SERIAL_CLOCK_RESET_MODULE,
    SERIAL_GTY_SERDES_QUAD_MODULE,
    SERIAL_PHY_MODULE,
    SERIAL_PHY_QUAD_MODULE,
)


SERIAL_PHY_PROVIDER_SCHEMA = "emuflow.serial-phy-provider/v1"
SERIAL_PHY_PROVIDER_V2_SCHEMA = "emuflow.serial-phy-provider/v2"
SERIAL_PHY_PROVIDER_V3_SCHEMA = "emuflow.serial-phy-provider/v3"
VALID_PROVIDER_QUALIFICATIONS = {
    "editable_source_hardware",
    "vendor_generated_hardware",
    "simulation_only",
}
VALID_SOURCE_LANGUAGES = {"systemverilog", "verilog", "tcl", "xdc"}
ALLOWED_SOURCE_SUFFIXES = {".sv", ".v", ".svh", ".vh", ".tcl", ".xdc"}
FORBIDDEN_SOURCE_SUFFIXES = {
    ".a", ".dcp", ".dll", ".edf", ".edf.gz", ".exe", ".lib", ".o",
    ".so", ".xci", ".xo", ".zip",
}


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _positive_number(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or float(value) <= 0.0
    ):
        raise ValidationError(f"{context}: expected a positive number")
    return float(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_platform_compatibility(
    provider: Mapping[str, Any], platform: Platform
) -> Dict[str, Any]:
    supported_parts = set(provider["supported_parts"])
    platform_parts = {fpga.part for fpga in platform.fpgas}
    unsupported = sorted(platform_parts - supported_parts)
    if unsupported:
        raise ValidationError(
            f"serial PHY provider does not support platform parts: {unsupported}"
        )
    serial_links = [link for link in platform.links if link.mode == "serial"]
    if not serial_links:
        raise ValidationError("platform has no serial link for the PHY provider")
    protocol = provider["protocol"]
    mismatches = []
    for link in serial_links:
        if (
            link.payload_bits_per_lane_per_cycle
            != protocol["payload_bits_per_lane_per_cycle"]
        ):
            mismatches.append(f"{link.id}:payload_width")
        if abs(link.fabric_clock_mhz - protocol["user_clock_mhz"]) > 1e-9:
            mismatches.append(f"{link.id}:user_clock")
        configured_rate = (
            link.fabric_clock_mhz
            * link.payload_bits_per_lane_per_cycle
            / 1000.0
        )
        if protocol["line_rate_gbps_per_lane"] + 1e-9 < configured_rate:
            mismatches.append(f"{link.id}:provider_line_rate_below_user_rate")
        if (
            link.max_line_rate_gbps_per_lane is not None
            and protocol["line_rate_gbps_per_lane"]
            > link.max_line_rate_gbps_per_lane * (1.0 + 1e-9)
        ):
            mismatches.append(f"{link.id}:provider_line_rate_above_board_ceiling")
    if mismatches:
        raise ValidationError(
            "serial PHY provider/BoardDB incompatibility: " + ", ".join(mismatches)
        )
    return {
        "platform": platform.name,
        "parts": sorted(platform_parts),
        "serial_links": len(serial_links),
        "status": "compatible",
    }


def validate_serial_phy_provider(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    platform: Optional[Platform] = None,
) -> Dict[str, Any]:
    schema = manifest.get("schema")
    if schema not in {
        SERIAL_PHY_PROVIDER_SCHEMA,
        SERIAL_PHY_PROVIDER_V2_SCHEMA,
        SERIAL_PHY_PROVIDER_V3_SCHEMA,
    }:
        raise ValidationError(
            "serial PHY provider schema must be "
            f"{SERIAL_PHY_PROVIDER_SCHEMA!r}, {SERIAL_PHY_PROVIDER_V2_SCHEMA!r}, "
            f"or {SERIAL_PHY_PROVIDER_V3_SCHEMA!r}"
        )
    provider_id = _string(manifest.get("id"), "provider.id")
    qualification = manifest.get("qualification")
    if qualification not in VALID_PROVIDER_QUALIFICATIONS:
        raise ValidationError("serial PHY provider qualification is invalid")
    raw_parts = manifest.get("supported_parts")
    if (
        not isinstance(raw_parts, list)
        or not raw_parts
        or any(not isinstance(part, str) or not part for part in raw_parts)
        or len(set(raw_parts)) != len(raw_parts)
    ):
        raise ValidationError("serial PHY provider supported_parts are invalid")
    modules = manifest.get("modules")
    if not isinstance(modules, dict):
        raise ValidationError("serial PHY provider modules are missing")
    if modules.get("clock_reset") != SERIAL_CLOCK_RESET_MODULE:
        raise ValidationError("serial PHY clock/reset module name is incompatible")
    if schema == SERIAL_PHY_PROVIDER_SCHEMA:
        if set(modules) != {"clock_reset", "lane"}:
            raise ValidationError("serial PHY v1 module inventory is incompatible")
        if modules.get("lane") != SERIAL_PHY_MODULE:
            raise ValidationError("serial PHY lane module name is incompatible")
        normalized_modules = {
            "clock_reset": SERIAL_CLOCK_RESET_MODULE,
            "lane": SERIAL_PHY_MODULE,
        }
    elif schema == SERIAL_PHY_PROVIDER_V2_SCHEMA:
        if set(modules) != {"clock_reset", "quad"}:
            raise ValidationError("serial PHY v2 module inventory is incompatible")
        if modules.get("quad") != SERIAL_PHY_QUAD_MODULE:
            raise ValidationError("serial PHY quad module name is incompatible")
        normalized_modules = {
            "clock_reset": SERIAL_CLOCK_RESET_MODULE,
            "quad": SERIAL_PHY_QUAD_MODULE,
        }
    else:
        if set(modules) != {"clock_reset", "serdes_quad"}:
            raise ValidationError("serial PHY v3 module inventory is incompatible")
        if modules.get("serdes_quad") != SERIAL_GTY_SERDES_QUAD_MODULE:
            raise ValidationError("serial PHY SerDes quad module name is incompatible")
        normalized_modules = {
            "clock_reset": SERIAL_CLOCK_RESET_MODULE,
            "serdes_quad": SERIAL_GTY_SERDES_QUAD_MODULE,
        }
    implementation = manifest.get("implementation")
    if not isinstance(implementation, dict):
        raise ValidationError("serial PHY implementation record is missing")
    implementation_kind = implementation.get("kind")
    if qualification in {"editable_source_hardware", "vendor_generated_hardware"}:
        if implementation_kind != "amd_ultrascale_plus_gty":
            raise ValidationError(
                "hardware PHY provider must declare amd_ultrascale_plus_gty"
            )
        normalized_implementation = {
            "kind": implementation_kind,
            "channel_primitive": _string(
                implementation.get("channel_primitive"),
                "implementation.channel_primitive",
            ),
            "reference_clock_primitive": _string(
                implementation.get("reference_clock_primitive"),
                "implementation.reference_clock_primitive",
            ),
            "reference_clock_instance": _string(
                implementation.get("reference_clock_instance"),
                "implementation.reference_clock_instance",
            ),
        }
        if schema == SERIAL_PHY_PROVIDER_SCHEMA:
            normalized_implementation["channel_instance"] = _string(
                implementation.get("channel_instance"),
                "implementation.channel_instance",
            )
        else:
            normalized_implementation.update(
                {
                    "common_primitive": _string(
                        implementation.get("common_primitive"),
                        "implementation.common_primitive",
                    ),
                    "common_instance": _string(
                        implementation.get("common_instance"),
                        "implementation.common_instance",
                    ),
                    "channel_instance_template": _string(
                        implementation.get("channel_instance_template"),
                        "implementation.channel_instance_template",
                    ),
                }
            )
            hierarchy_resolution = implementation.get(
                "hierarchy_resolution", "exact_instance"
            )
            if hierarchy_resolution not in {
                "exact_instance",
                "descendant_primitive_sorted",
            }:
                raise ValidationError(
                    "hardware PHY hierarchy_resolution is invalid"
                )
            normalized_implementation["hierarchy_resolution"] = (
                hierarchy_resolution
            )
            template = normalized_implementation["channel_instance_template"]
            if template.count("{channel}") != 1:
                raise ValidationError(
                    "hardware PHY channel_instance_template must contain "
                    "exactly one {channel} placeholder"
                )
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", primitive) is None
            for primitive in (
                normalized_implementation["channel_primitive"],
                normalized_implementation["reference_clock_primitive"],
                normalized_implementation["reference_clock_instance"],
                *(
                    (normalized_implementation["channel_instance"],)
                    if schema == SERIAL_PHY_PROVIDER_SCHEMA
                    else (
                        normalized_implementation["common_primitive"],
                        normalized_implementation["common_instance"],
                    )
                ),
            )
        ):
            raise ValidationError("hardware PHY primitive name is invalid")
        if schema != SERIAL_PHY_PROVIDER_SCHEMA and re.fullmatch(
            r"[A-Za-z0-9_{}./\[\]]+",
            normalized_implementation["channel_instance_template"],
        ) is None:
            raise ValidationError("hardware PHY channel hierarchy template is invalid")
    else:
        if implementation_kind != "behavioral":
            raise ValidationError(
                "simulation PHY provider implementation must be behavioral"
            )
        normalized_implementation = {"kind": "behavioral"}

    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValidationError("serial PHY protocol record is missing")
    normalized_protocol = {
        "payload_bits_per_lane_per_cycle": protocol.get(
            "payload_bits_per_lane_per_cycle"
        ),
        "user_clock_mhz": _positive_number(
            protocol.get("user_clock_mhz"), "protocol.user_clock_mhz"
        ),
        "line_rate_gbps_per_lane": _positive_number(
            protocol.get("line_rate_gbps_per_lane"),
            "protocol.line_rate_gbps_per_lane",
        ),
        "encoding": _string(protocol.get("encoding"), "protocol.encoding"),
        "link_training": _string(
            protocol.get("link_training"), "protocol.link_training"
        ),
        "reset_sequence": _string(
            protocol.get("reset_sequence"), "protocol.reset_sequence"
        ),
    }
    if schema == SERIAL_PHY_PROVIDER_V3_SCHEMA:
        normalized_protocol.update(
            {
                "pcs_data_width": protocol.get("pcs_data_width"),
                "pcs_header_width": protocol.get("pcs_header_width"),
                "pcs_clock_mhz": _positive_number(
                    protocol.get("pcs_clock_mhz"), "protocol.pcs_clock_mhz"
                ),
                "pcs_implementation": _string(
                    protocol.get("pcs_implementation"),
                    "protocol.pcs_implementation",
                ),
            }
        )
        if (
            normalized_protocol["pcs_data_width"] != 64
            or normalized_protocol["pcs_header_width"] != 2
            or normalized_protocol["encoding"] != "64b66b"
            or normalized_protocol["pcs_implementation"]
            != "emuflow-in-tree-corundum-10gbase-r"
        ):
            raise ValidationError("serial PHY v3 PCS boundary is incompatible")
        expected_line_rate = (
            normalized_protocol["pcs_clock_mhz"] * 66.0 / 1000.0
        )
        if abs(
            normalized_protocol["line_rate_gbps_per_lane"] - expected_line_rate
        ) > 1e-9:
            raise ValidationError(
                "serial PHY v3 line rate must equal pcs_clock_mhz * 66 bits"
            )
        # The selected in-tree framer accepts one 64-bit record every three
        # PCS clocks (HEADER/BODY/TERM), not one record per 66-bit wire block.
        # This necessary nominal-rate bound does not qualify CDC margins,
        # control traffic, training, or board latency.
        if normalized_protocol["payload_bits_per_lane_per_cycle"] != 64:
            raise ValidationError("serial PHY v3 record payload must be 64 bits")
        if (normalized_protocol["user_clock_mhz"] * 3.0
                > normalized_protocol["pcs_clock_mhz"] + 1e-9):
            raise ValidationError(
                "serial PHY v3 user rate exceeds three-cycle PCS record capacity"
            )
    payload_width = normalized_protocol["payload_bits_per_lane_per_cycle"]
    if (
        isinstance(payload_width, bool)
        or not isinstance(payload_width, int)
        or payload_width <= 0
    ):
        raise ValidationError(
            "protocol.payload_bits_per_lane_per_cycle must be a positive integer"
        )

    raw_root = _string(manifest.get("source_root"), "provider.source_root")
    root = (manifest_path.parent / raw_root).resolve()
    if not root.is_dir():
        raise ValidationError("serial PHY provider source_root does not exist")
    raw_sources = manifest.get("sources")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or any(not isinstance(item, dict) for item in raw_sources)
    ):
        raise ValidationError("serial PHY provider sources are invalid")
    inventory = []
    declared_paths = set()
    hdl_text = []
    for index, item in enumerate(raw_sources):
        context = f"provider.sources[{index}]"
        relative = _string(item.get("path"), f"{context}.path")
        language = item.get("language")
        role = _string(item.get("role"), f"{context}.role")
        expected = _string(item.get("sha256"), f"{context}.sha256")
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValidationError(f"{context}.sha256 is invalid")
        if language not in VALID_SOURCE_LANGUAGES:
            raise ValidationError(f"{context}.language is invalid")
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValidationError(f"{context}: source escapes or is missing")
        suffixes = "".join(path.suffixes).lower()
        if (
            path.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES
            or any(suffixes.endswith(suffix) for suffix in FORBIDDEN_SOURCE_SUFFIXES)
        ):
            raise ValidationError(f"{context}: opaque provider artifact is forbidden")
        if relative in declared_paths:
            raise ValidationError(f"{context}: duplicate source path")
        actual = _sha256(path)
        if actual != expected:
            raise ValidationError(f"{context}: source SHA-256 mismatch")
        declared_paths.add(relative)
        inventory.append(
            {
                "path": relative,
                "language": language,
                "role": role,
                "bytes": path.stat().st_size,
                "sha256": actual,
            }
        )
        if language in {"systemverilog", "verilog"}:
            hdl_text.append(path.read_text(encoding="utf-8"))
    combined_hdl = "\n".join(hdl_text)
    if (
        qualification in {"editable_source_hardware", "vendor_generated_hardware"}
        and re.search(r"\(\*\s*black_box\s*\*\)", combined_hdl)
    ):
        raise ValidationError(
            "editable-source hardware provider cannot contain black-box modules"
        )
    if qualification == "editable_source_hardware":
        channel_instance = (
            normalized_implementation["channel_instance"]
            if schema == SERIAL_PHY_PROVIDER_SCHEMA
            else re.split(
                r"[/.]", normalized_implementation["channel_instance_template"]
            )[-1]
        )
        required_instances = {
            normalized_implementation["channel_primitive"]: channel_instance,
            normalized_implementation["reference_clock_primitive"]: (
                normalized_implementation["reference_clock_instance"]
            ),
        }
        if schema != SERIAL_PHY_PROVIDER_SCHEMA:
            required_instances[normalized_implementation["common_primitive"]] = (
                normalized_implementation["common_instance"]
            )
        missing_instances = sorted(
            f"{primitive}:{instance}"
            for primitive, instance in required_instances.items()
            if re.search(
                rf"\b{re.escape(primitive)}\s+"
                rf"(?:#\s*\([^;]*?\)\s*)?{re.escape(instance)}\s*\(",
                combined_hdl,
                re.DOTALL,
            )
            is None
        )
        if missing_instances:
            raise ValidationError(
                "editable-source hardware provider omits declared primitive "
                f"instances: {missing_instances}"
            )
    vendor_products = None
    if qualification == "vendor_generated_hardware":
        raw_products = manifest.get("vendor_products")
        if not isinstance(raw_products, dict):
            raise ValidationError("vendor-generated provider products are missing")
        if raw_products.get("generator") != "vivado_gtwizard_ultrascale":
            raise ValidationError("vendor-generated provider generator is invalid")
        raw_xci = raw_products.get("xci")
        if (
            not isinstance(raw_xci, list)
            or not raw_xci
            or any(not isinstance(item, dict) for item in raw_xci)
        ):
            raise ValidationError("vendor-generated provider XCI inventory is invalid")
        xci_inventory = []
        xci_paths = set()
        for index, item in enumerate(raw_xci):
            context = f"vendor_products.xci[{index}]"
            relative = _string(item.get("path"), f"{context}.path")
            expected = _string(item.get("sha256"), f"{context}.sha256")
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                raise ValidationError(f"{context}.sha256 is invalid")
            path = (manifest_path.parent / relative).resolve()
            if (
                manifest_path.parent.resolve() not in path.parents
                or not path.is_file()
                or path.suffix.lower() != ".xci"
            ):
                raise ValidationError(f"{context}: XCI escapes or is missing")
            if relative in xci_paths or _sha256(path) != expected:
                raise ValidationError(f"{context}: duplicate or SHA-256 mismatch")
            xci_paths.add(relative)
            xci_inventory.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": expected,
                }
            )
        raw_modules = raw_products.get("modules")
        if (
            not isinstance(raw_modules, list)
            or not raw_modules
            or any(
                not isinstance(module, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module) is None
                for module in raw_modules
            )
            or len(raw_modules) != len(set(raw_modules))
        ):
            raise ValidationError(
                "vendor-generated provider module inventory is invalid"
            )
        missing_modules = sorted(
            module
            for module in raw_modules
            if re.search(rf"\b{re.escape(module)}\s+\w+\s*\(", combined_hdl)
            is None
        )
        if missing_modules:
            raise ValidationError(
                "provider adapter omits generated IP modules: "
                + ", ".join(missing_modules)
            )
        vendor_products = {
            "generator": "vivado_gtwizard_ultrascale",
            "modules": sorted(raw_modules),
            "xci": sorted(xci_inventory, key=lambda item: item["path"]),
            "counts_as_open_flow_implementation": False,
        }
    for role, module in sorted(modules.items()):
        if re.search(rf"\bmodule\s+{re.escape(module)}\b", combined_hdl) is None:
            raise ValidationError(
                f"serial PHY provider source does not define {role} module {module}"
            )

    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("serial PHY provider provenance is missing")
    license_id = _string(provenance.get("license"), "provenance.license")
    upstream = _string(provenance.get("upstream"), "provenance.upstream")
    normalized = {
        "schema": schema,
        "id": provider_id,
        "qualification": qualification,
        "supported_parts": sorted(raw_parts),
        "modules": normalized_modules,
        "implementation": normalized_implementation,
        "source_root": raw_root,
        "sources": sorted(inventory, key=lambda item: item["path"]),
        "protocol": normalized_protocol,
        "provenance": {"license": license_id, "upstream": upstream},
        **({"vendor_products": vendor_products} if vendor_products else {}),
    }
    compatibility = (
        _validate_platform_compatibility(normalized, platform)
        if platform is not None
        else None
    )
    return {
        "status": "pass",
        "provider": provider_id,
        "qualification": qualification,
        "editable_sources": len(inventory),
        "source_bytes": sum(item["bytes"] for item in inventory),
        "compatibility": compatibility,
        "normalized": normalized,
    }


def validate_serial_phy_provider_file(
    manifest_path: Path,
    platform_path: Optional[Path] = None,
    normalized_out: Optional[Path] = None,
) -> Dict[str, Any]:
    platform = Platform.load(platform_path) if platform_path is not None else None
    result = validate_serial_phy_provider(
        read_json(manifest_path), manifest_path, platform
    )
    if normalized_out is not None:
        write_json(normalized_out, result["normalized"])
    return {key: value for key, value in result.items() if key != "normalized"}
