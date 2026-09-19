"""Explicit RapidWright device-provider contract for the Route A backend."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .architecture import ArchitectureDB
from .errors import ValidationError
from .fpga_interchange import (
    parse_xilinx_part_identity,
    run_fpga_interchange_architecture_import,
    validate_fpga_interchange_architecture,
)
from .io import read_json, write_json
from .physical_regions import validate_fpga_interchange_architecture_regions


RAPIDWRIGHT_PROVIDER_SCHEMA = "emuflow.rapidwright-device-provider/v1"
RAPIDWRIGHT_PROVIDER_ID = "rapidwright-xilinx-device-v1"
RAPIDWRIGHT_ROUTE_CERTIFICATE_SCHEMA = (
    "emuflow.rapidwright-route-resource-certificate/v1"
)
RAPIDWRIGHT_ROUTE_BACKEND = "rapidwright-native-device-database-v1"
RAPIDWRIGHT_GENERATOR_QUALIFICATION = "mixed-license-external"
_SUPPORTED_LICENSE_QUALIFICATION = {
    "source_code": "Apache-2.0",
    "device_data": "Xilinx-EULA",
    "runtime": RAPIDWRIGHT_GENERATOR_QUALIFICATION,
    "redistribution": "external-dependency-not-redistributed",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _positive_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{context}: expected a positive integer")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{context}: expected a boolean")
    return value


def validate_rapidwright_provider_manifest(
    manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate a complete, redistribution-safe external provider identity."""
    if manifest.get("schema") != RAPIDWRIGHT_PROVIDER_SCHEMA:
        raise ValidationError("RapidWright provider manifest schema is invalid")
    if manifest.get("provider_id") != RAPIDWRIGHT_PROVIDER_ID:
        raise ValidationError("RapidWright provider id is invalid")

    release = manifest.get("release")
    if not isinstance(release, dict):
        raise ValidationError("RapidWright provider release is missing")
    version = _string(release.get("version"), "manifest.release.version")
    tag = _string(release.get("tag"), "manifest.release.tag")
    revision = _string(
        release.get("revision"), "manifest.release.revision"
    ).lower()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValidationError(
            "manifest.release.revision: expected a full Git commit"
        )
    if tag != f"v{version}-beta":
        raise ValidationError(
            "manifest.release.tag must explicitly match the beta release version"
        )

    device = manifest.get("device")
    if not isinstance(device, dict):
        raise ValidationError("RapidWright provider device is missing")
    identity = parse_xilinx_part_identity(
        _string(device.get("part"), "manifest.device.part")
    )
    for field in (
        "device",
        "package",
        "speed_grade",
        "temperature_grade",
    ):
        value = _string(device.get(field), f"manifest.device.{field}")
        if value.upper() != identity[field].upper():
            raise ValidationError(
                f"manifest.device.{field} does not match manifest.device.part"
            )

    interchange = manifest.get("fpga_interchange")
    if not isinstance(interchange, dict):
        raise ValidationError("RapidWright FPGA Interchange identity is missing")
    schema_revision = _string(
        interchange.get("schema_revision"),
        "manifest.fpga_interchange.schema_revision",
    ).lower()
    if len(schema_revision) != 40 or any(
        char not in "0123456789abcdef" for char in schema_revision
    ):
        raise ValidationError(
            "manifest.fpga_interchange.schema_revision: expected a full Git commit"
        )
    if interchange.get("schema_license") != "Apache-2.0":
        raise ValidationError(
            "manifest.fpga_interchange.schema_license must be Apache-2.0"
        )
    generator_class = _string(
        interchange.get("generator_class"),
        "manifest.fpga_interchange.generator_class",
    )
    native_database = manifest.get("rapidwright_device_database")
    if not isinstance(native_database, dict):
        raise ValidationError(
            "manifest.rapidwright_device_database is missing"
        )
    database_resource = _string(
        native_database.get("resource"),
        "manifest.rapidwright_device_database.resource",
    )
    database_md5 = _string(
        native_database.get("md5"),
        "manifest.rapidwright_device_database.md5",
    ).lower()
    if len(database_md5) != 32 or any(
        character not in "0123456789abcdef" for character in database_md5
    ):
        raise ValidationError(
            "manifest.rapidwright_device_database.md5: expected MD5 hex"
        )
    if native_database.get("redistribution") != (
        "external-dependency-not-redistributed"
    ):
        raise ValidationError(
            "manifest.rapidwright_device_database.redistribution is invalid"
        )

    license_contract = manifest.get("license")
    if not isinstance(license_contract, dict):
        raise ValidationError("RapidWright provider license contract is missing")
    for field, expected in _SUPPORTED_LICENSE_QUALIFICATION.items():
        if license_contract.get(field) != expected:
            raise ValidationError(
                f"manifest.license.{field}: expected {expected!r}"
            )
    if _boolean(
        license_contract.get("generated_device_data_committable"),
        "manifest.license.generated_device_data_committable",
    ):
        raise ValidationError(
            "RapidWright generated device data must remain outside source control"
        )

    expected_resources = manifest.get("expected_physical_resources")
    if not isinstance(expected_resources, dict) or not expected_resources:
        raise ValidationError(
            "manifest.expected_physical_resources: expected an object"
        )
    normalized_resources = {
        _string(name, "manifest.expected_physical_resources key"): (
            _positive_integer(
                count,
                f"manifest.expected_physical_resources[{name!r}]",
            )
        )
        for name, count in expected_resources.items()
    }
    evidence = manifest.get("resource_evidence")
    if not isinstance(evidence, dict):
        raise ValidationError("manifest.resource_evidence is missing")
    normalized_evidence = {
        field: _string(
            evidence.get(field), f"manifest.resource_evidence.{field}"
        )
        for field in ("document", "revision", "url", "table")
    }

    return {
        "status": "pass",
        "schema": RAPIDWRIGHT_PROVIDER_SCHEMA,
        "provider_id": RAPIDWRIGHT_PROVIDER_ID,
        "version": version,
        "tag": tag,
        "revision": revision,
        "part": identity["part"],
        "device_identity": identity,
        "schema_revision": schema_revision,
        "generator_class": generator_class,
        "device_database_resource": database_resource,
        "device_database_md5": database_md5,
        "license_qualification": RAPIDWRIGHT_GENERATOR_QUALIFICATION,
        "expected_physical_resources": dict(
            sorted(normalized_resources.items())
        ),
        "resource_evidence": normalized_evidence,
    }


def validate_rapidwright_route_certificate(
    certificate: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
) -> Dict[str, Any]:
    """Validate a compact certificate for RapidWright's native route graph."""
    checked_manifest = validate_rapidwright_provider_manifest(manifest)
    if certificate.get("schema") != RAPIDWRIGHT_ROUTE_CERTIFICATE_SCHEMA:
        raise ValidationError(
            "RapidWright route certificate schema is invalid"
        )
    payload = certificate.get("payload")
    if not isinstance(payload, dict):
        raise ValidationError(
            "RapidWright route certificate payload is missing"
        )
    payload_sha256 = _string(
        certificate.get("payload_sha256"),
        "route_certificate.payload_sha256",
    ).lower()
    if payload_sha256 != _canonical_sha256(payload):
        raise ValidationError("RapidWright route certificate digest mismatch")

    expected_strings = {
        "device": checked_manifest["device_identity"]["device"],
        "full_part": checked_manifest["part"],
        "device_database_md5": checked_manifest["device_database_md5"],
        "provider_manifest_sha256": _sha256(manifest_path),
        "route_backend": RAPIDWRIGHT_ROUTE_BACKEND,
        "timing_qualification": (
            "not-encoded-rwroute-native-device-database"
        ),
    }
    for field, expected in expected_strings.items():
        if payload.get(field) != expected:
            raise ValidationError(
                f"RapidWright route certificate field {field!r} does not "
                "match the provider manifest"
            )
    generator = payload.get("generator")
    if not isinstance(generator, dict) or generator != {
        "revision": checked_manifest["revision"],
        "version": checked_manifest["version"],
    }:
        raise ValidationError(
            "RapidWright route certificate generator identity is invalid"
        )
    integrity = payload.get("reference_integrity")
    if integrity != {
        "errors": 0,
        "route_resources_present": True,
        "status": "pass",
    }:
        raise ValidationError(
            "RapidWright route certificate reference integrity failed"
        )
    counts = payload.get("resource_counts")
    required_counts = {
        "all_sites",
        "all_tiles",
        "node_wire_memberships",
        "nodes",
        "pips",
        "site_types",
        "tile_types",
        "wires",
        "wires_unaccounted",
        "wires_without_node",
    }
    if not isinstance(counts, dict) or set(counts) != required_counts:
        raise ValidationError(
            "RapidWright route certificate resource counts are incomplete"
        )
    for field in sorted(required_counts):
        value = counts[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (
                field not in {"wires_without_node", "wires_unaccounted"}
                and value == 0
            )
        ):
            raise ValidationError(
                f"route_certificate.resource_counts.{field}: invalid count"
            )
    if (
        counts["node_wire_memberships"]
        + counts["wires_without_node"]
        + counts["wires_unaccounted"]
        != counts["wires"]
    ):
        raise ValidationError(
            "RapidWright route certificate does not account for every wire"
        )
    return {
        "status": "pass",
        "schema": RAPIDWRIGHT_ROUTE_CERTIFICATE_SCHEMA,
        "payload_sha256": payload_sha256,
        "resource_counts": dict(sorted(counts.items())),
        "route_backend": RAPIDWRIGHT_ROUTE_BACKEND,
        "timing_qualification": payload["timing_qualification"],
    }


def load_rapidwright_route_certificate(
    path: Path,
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError(
            "RapidWright route certificate must be an object"
        )
    checked = validate_rapidwright_route_certificate(
        value, manifest, manifest_path=manifest_path
    )
    return value, checked


def bind_rapidwright_route_certificate(
    architecture: ArchitectureDB,
    certificate: Mapping[str, Any],
    checked_certificate: Mapping[str, Any],
    *,
    certificate_path: Path,
) -> ArchitectureDB:
    """Bind an independently checked native route graph to ArchitectureDB."""
    value = architecture.to_dict()
    static_counts = value.get("routing_resource_counts")
    if not isinstance(static_counts, dict):
        raise ValidationError(
            "ArchitectureDB static resource counts are missing"
        )
    route_counts = checked_certificate["resource_counts"]
    for route_field, static_field in (
        ("all_tiles", "all_tiles"),
        ("site_types", "site_types"),
        ("tile_types", "tile_types"),
    ):
        if route_counts[route_field] != static_counts.get(static_field):
            raise ValidationError(
                "RapidWright route certificate does not match the static "
                f"DeviceResources count {static_field!r}"
            )
    value["routing_resource_counts"] = {
        **static_counts,
        **dict(route_counts),
    }
    value["routing_reference_integrity"] = {
        "status": "pass",
        "route_resources_present": True,
        "route_timings_present": False,
        "site_types": route_counts["site_types"],
        "tile_types": route_counts["tile_types"],
        "tiles": route_counts["all_tiles"],
        "wires": route_counts["wires"],
        "nodes": route_counts["nodes"],
        "pips": route_counts["pips"],
        "node_wire_memberships": route_counts["node_wire_memberships"],
        "wires_unaccounted": route_counts["wires_unaccounted"],
        "wires_without_node_membership": route_counts[
            "wires_without_node"
        ],
    }
    value["routing_resource_provider"] = {
        "mode": RAPIDWRIGHT_ROUTE_BACKEND,
        "certificate_schema": RAPIDWRIGHT_ROUTE_CERTIFICATE_SCHEMA,
        "certificate_sha256": _sha256(certificate_path),
        "certificate_payload_sha256": checked_certificate["payload_sha256"],
        "device_database_md5": certificate["payload"][
            "device_database_md5"
        ],
        "timing_qualification": checked_certificate[
            "timing_qualification"
        ],
    }
    return ArchitectureDB(value)


def load_rapidwright_provider_manifest(path: Path) -> Dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("RapidWright provider manifest must be an object")
    validate_rapidwright_provider_manifest(value)
    return value


def rapidwright_producer_record(
    manifest: Mapping[str, Any], manifest_path: Path
) -> Dict[str, Any]:
    checked = validate_rapidwright_provider_manifest(manifest)
    return {
        "provider_id": checked["provider_id"],
        "name": "RapidWright",
        "version": checked["version"],
        "release_tag": checked["tag"],
        "revision": checked["revision"],
        "provider_manifest_schema": RAPIDWRIGHT_PROVIDER_SCHEMA,
        "provider_manifest_sha256": _sha256(manifest_path),
        "license_qualification": checked["license_qualification"],
    }


def validate_rapidwright_architecture(
    architecture: ArchitectureDB,
    manifest: Mapping[str, Any],
    *,
    manifest_path: Optional[Path] = None,
    require_physical_regions: bool = False,
) -> Dict[str, Any]:
    """Validate the imported device against its exact provider contract."""
    checked_manifest = validate_rapidwright_provider_manifest(manifest)
    checked_architecture = validate_fpga_interchange_architecture(architecture)
    if architecture.part != checked_manifest["part"]:
        raise ValidationError(
            "RapidWright ArchitectureDB part does not match provider manifest"
        )
    producer = architecture.value["source"].get("producer")
    if not isinstance(producer, dict):
        raise ValidationError("RapidWright ArchitectureDB producer is missing")
    for field in (
        "provider_id",
        "version",
        "release_tag",
        "revision",
        "license_qualification",
    ):
        expected_field = "tag" if field == "release_tag" else field
        if producer.get(field) != checked_manifest[expected_field]:
            raise ValidationError(
                f"RapidWright ArchitectureDB producer field {field!r} "
                "does not match provider manifest"
            )
    if manifest_path is not None and producer.get(
        "provider_manifest_sha256"
    ) != _sha256(manifest_path):
        raise ValidationError(
            "RapidWright ArchitectureDB is bound to a different provider manifest"
        )

    route_provider = architecture.value.get("routing_resource_provider")
    if not isinstance(route_provider, dict):
        raise ValidationError(
            "RapidWright ArchitectureDB native route provider is missing"
        )
    if route_provider.get("mode") != RAPIDWRIGHT_ROUTE_BACKEND:
        raise ValidationError(
            "RapidWright ArchitectureDB native route backend is invalid"
        )
    if route_provider.get("device_database_md5") != checked_manifest[
        "device_database_md5"
    ]:
        raise ValidationError(
            "RapidWright ArchitectureDB is bound to a different device database"
        )

    slots = architecture.summary()["cell_slots"]
    # DS890's CLB register count excludes the dedicated Laguna crossing
    # registers.  FPGA Interchange correctly exposes those BELs as FDRE-
    # compatible, so a device-wide compatible-cell sum is intentionally
    # larger than CLB_FF.  Count only SLICEL/SLICEM for the data-sheet CLB
    # inventory check while retaining Laguna sites in the exact device model.
    slice_slots: Dict[str, int] = {}
    templates = architecture.value.get("site_templates", {})
    for site in architecture.value["sites"]:
        if site.get("type") not in {"SLICEL", "SLICEM"}:
            continue
        bels = site.get("bels")
        if bels is None:
            template = templates.get(site.get("template"), {})
            bels = template.get("bels", [])
        for bel in bels:
            for cell in bel.get("compatible_cells", []):
                slice_slots[cell] = slice_slots.get(cell, 0) + 1
    observed_resources = {
        "CLB_LUT": slice_slots.get("LUT6", 0),
        "CLB_FF": slice_slots.get("FDRE", 0),
        "CARRY8": slots.get("CARRY8", 0),
        "DSP48E2": slots.get("DSP48E2", 0),
        "RAMB18E2": slots.get("RAMB18E2", 0),
        "RAMB36E2": slots.get("RAMB36E2", 0),
        "URAM288": slots.get("URAM288", 0),
    }
    expected_resources = checked_manifest["expected_physical_resources"]
    mismatches = {
        resource: {
            "expected": expected,
            "observed": observed_resources.get(resource, 0),
        }
        for resource, expected in expected_resources.items()
        if observed_resources.get(resource, 0) != expected
    }
    if mismatches:
        raise ValidationError(
            "RapidWright ArchitectureDB physical resource inventory does not "
            f"match the provider manifest: {mismatches}"
        )

    regions = None
    if require_physical_regions:
        regions = validate_fpga_interchange_architecture_regions(architecture)
    return {
        "status": "pass",
        "provider": checked_manifest,
        "architecture": checked_architecture,
        "physical_resources": dict(sorted(observed_resources.items())),
        "physical_regions": regions,
    }


def run_rapidwright_device_import(
    *,
    input_path: Path,
    provider_manifest_path: Path,
    route_certificate_path: Path,
    output_path: Path,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Import DeviceResources with an explicit RapidWright identity."""
    manifest = load_rapidwright_provider_manifest(provider_manifest_path)
    checked = validate_rapidwright_provider_manifest(manifest)
    producer = rapidwright_producer_record(manifest, provider_manifest_path)
    certificate, checked_certificate = load_rapidwright_route_certificate(
        route_certificate_path,
        manifest,
        manifest_path=provider_manifest_path,
    )
    report = run_fpga_interchange_architecture_import(
        input_path=input_path,
        part=checked["part"],
        generator=(
            f"RapidWright {checked['version']} "
            f"({checked['revision']})"
        ),
        generator_qualification=RAPIDWRIGHT_GENERATOR_QUALIFICATION,
        producer=producer,
        output_path=output_path,
        executable=executable,
        log_path=log_path,
    )
    architecture = bind_rapidwright_route_certificate(
        ArchitectureDB.load(output_path),
        certificate,
        checked_certificate,
        certificate_path=route_certificate_path,
    )
    write_json(output_path, architecture.to_dict())
    validation = validate_rapidwright_architecture(
        architecture,
        manifest,
        manifest_path=provider_manifest_path,
    )
    return {
        **report,
        "checker": validation["architecture"],
        "route_certificate": dict(checked_certificate),
        "rapidwright_validation": validation,
    }
