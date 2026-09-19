"""Explicit RapidWright device-provider contract for the Route A backend."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .architecture import ArchitectureDB
from .errors import ValidationError
from .fpga_interchange import (
    parse_xilinx_part_identity,
    run_fpga_interchange_architecture_import,
    validate_fpga_interchange_architecture,
)
from .io import read_json
from .physical_regions import validate_fpga_interchange_architecture_regions


RAPIDWRIGHT_PROVIDER_SCHEMA = "emuflow.rapidwright-device-provider/v1"
RAPIDWRIGHT_PROVIDER_ID = "rapidwright-xilinx-device-v1"
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
        "license_qualification": RAPIDWRIGHT_GENERATOR_QUALIFICATION,
        "expected_physical_resources": dict(
            sorted(normalized_resources.items())
        ),
        "resource_evidence": normalized_evidence,
    }


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

    slots = architecture.summary()["cell_slots"]
    observed_resources = {
        "CLB_LUT": slots.get("LUT6", 0),
        "CLB_FF": slots.get("FDRE", 0),
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
    output_path: Path,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Import DeviceResources with an explicit RapidWright identity."""
    manifest = load_rapidwright_provider_manifest(provider_manifest_path)
    checked = validate_rapidwright_provider_manifest(manifest)
    producer = rapidwright_producer_record(manifest, provider_manifest_path)
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
    validation = validate_rapidwright_architecture(
        ArchitectureDB.load(output_path),
        manifest,
        manifest_path=provider_manifest_path,
    )
    return {**report, "rapidwright_validation": validation}
