"""Bounded source/reference host for the sole physical handoff producer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.control_document import read_small_object
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import (
    native_file_generation,
    require_native_schema,
    resolve_native_executable,
)
from emuflow.storage.store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
)
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS = (
    "device",
    "inode",
    "bytes",
    "mtime_seconds",
    "mtime_nanoseconds",
    "ctime_seconds",
    "ctime_nanoseconds",
)




def _payload(selector, descriptor, key):
    value = descriptor.get(key)
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValidationError("physical handoff payload ownership is invalid")
    return Path(selector).resolve().with_name(value)


def _write_request(path, sources, config):
    """Only source identities and bounded top selection cross the host."""
    if not 6 <= len(sources) <= 16:
        raise ValidationError("physical handoff source catalog exceeds bound")
    writer = CheckedRequestWriter(path)
    try:
        writer.put("QII", 0x31424850464D5545, 1, len(sources))
        for role, source in sources:
            writer.text(role)
            writer.text(str(source.path))
            writer.text(source.schema)
            writer.put("3Q4q", *source.generation)
        writer.text(
            json.dumps(
                config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        writer.finish()
    finally:
        writer.close()


def _sources(
    mapped_path,
    architecture_path,
    certificate_path,
    source_path,
    *,
    constraints_path,
    native_constraints_path,
    provider_manifest_path,
    mapped_value,
    source_packed_value,
):
    if (native_constraints_path is None) != (provider_manifest_path is None):
        raise ValidationError(
            "physical handoff requires both native constraints and provider"
        )
    if source_path is None:
        raise ValidationError(
            "native physical handoff requires its typed atomic-source owner"
        )
    mapped_path = Path(mapped_path).resolve()
    if mapped_value is not None:
        if (
            getattr(mapped_value, "schema", None) != "emuflow.xilinx-mapped-index/v2"
            or Path(mapped_value.path).resolve() != mapped_path
        ):
            raise ValidationError("physical handoff borrowed mapped owner disagrees")
        mapped_value.require_source_unchanged()
    mapped = bind_store_source(mapped_path, "xilinx-mapped-index-v2")
    certificate_path, source_path = (
        Path(certificate_path).resolve(),
        Path(source_path).resolve(),
    )
    certificate_selector = bind_store_source(
        certificate_path, "openparf-placement-selector-v2"
    )
    atomic_selector = bind_store_source(
        source_path, "openparf-atomic-source-selector-v1"
    )
    certificate, atomic = read_small_object(certificate_path), read_small_object(source_path)
    if "clusters" in certificate:
        raise ValidationError(
            "production physical handoff requires typed placement certificate; inline clusters are retired"
        )
    if atomic.get("schema") != "emuflow.openparf-atomic-source-storage/v1":
        raise ValidationError(
            "production physical handoff requires the typed atomic-source owner"
        )
    if source_packed_value is not None:
        require = getattr(source_packed_value, "require_sources_unchanged", None)
        if (
            not callable(require)
            or getattr(source_packed_value, "selector", None) != atomic
        ):
            raise ValidationError("physical handoff borrowed atomic source disagrees")
        require()
    sources = [
        ("mapped", mapped),
        (
            "certificate",
            bind_store_source(
                _payload(
                    certificate_path, certificate.get("cluster_storage", {}), "file"
                ),
                "openparf-placement-certificate-v3",
            ),
        ),
        ("certificate_selector", certificate_selector),
        (
            "atomic_source",
            bind_store_source(
                _payload(source_path, atomic, "artifact"),
                "openparf-atomic-source-store-v1",
            ),
        ),
        ("atomic_selector", atomic_selector),
        (
            "architecture",
            bind_store_source(
                Path(architecture_path).resolve(), "physical-input-document-v1"
            ),
        ),
    ]
    for role, path in (
        ("constraints", constraints_path),
        ("native", native_constraints_path),
        ("provider", provider_manifest_path),
    ):
        if path is not None:
            sources.append(
                (
                    role,
                    bind_store_source(
                        Path(path).resolve(), "physical-input-document-v1"
                    ),
                )
            )
    require_store_sources_unchanged(tuple(source for _, source in sources))
    return tuple(sources)


def materialize_xilinx_openparf_atomic_contract(
    mapped_path,
    architecture_path,
    atomic_placement_path,
    physical_handoff_path,
    *,
    top=None,
    source_packed_path=None,
    source_packed_value=None,
    constraints_path=None,
    native_constraints_path=None,
    provider_manifest_path=None,
    mapped_value=None,
    mapped_sha256=None,
    packed_value_sink=None,
    placement_value_sink=None,
):
    require_native_schema("physical-handoff-bridge-request-v1")
    if top is not None and (not isinstance(top, str) or not top):
        raise ValidationError("physical handoff top selection is invalid")
    sources = _sources(
        mapped_path,
        architecture_path,
        atomic_placement_path,
        source_packed_path,
        constraints_path=constraints_path,
        native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,
        source_packed_value=source_packed_value,
    )
    target = Path(physical_handoff_path).resolve()
    for _, source in sources:
        if target == source.path or (
            target.exists() and os.path.samefile(target, source.path)
        ):
            raise ValidationError("physical handoff output aliases an input")
    target.parent.mkdir(parents=True, exist_ok=True)
    generation = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".physical-handoff-host-", dir=target.parent
        ) as temporary:
            root = Path(temporary)
            request, proof = root / "request.bin", root / "operation.json"
            _write_request(
                request,
                sources,
                {"schema": "emuflow.physical-handoff-build/v1", "top": top or ""},
            )
            generation = Path(
                tempfile.mkdtemp(prefix=target.name + ".generation.", dir=target.parent)
            )
            generation.rmdir()
            leases = tuple(source for _, source in sources)
            require_store_sources_unchanged(leases)
            executable = resolve_native_executable("emuflow-native")
            command = [
                executable,
                "--report",
                str(proof),
                "physical",
                "build-handoff",
                str(request),
                str(generation),
            ]
            with NativeToolProcess(
                executable, command, environment=dict(os.environ), scratch_parent=root
            ) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError(
                    "native physical handoff producer failed: " + completed.stdout
                )
            result = read_small_object(proof, 262144)
            outputs = result.get("outputs", [])
            if (
                result.get("stage") != "physical.build-handoff"
                or result.get("status") != "pass"
                or len(outputs) != 2
            ):
                raise ValidationError(
                    "native physical handoff producer proof disagrees"
                )
            output_sources = []
            for row, (name, schema) in zip(
                outputs,
                (
                    ("physical-handoff.sqlite", "xilinx-physical-handoff-v1"),
                    (
                        "physical-bridge-report.json",
                        "physical-handoff-bridge-report-v1",
                    ),
                ),
            ):
                path = generation / name
                observed = tuple(row.get("generation", {}).get(key) for key in _FIELDS)
                if (
                    row.get("schema") != schema
                    or row.get("path") != str(path)
                    or path.is_symlink()
                    or any(type(value) is not int for value in observed)
                    or observed != native_file_generation(path)
                ):
                    raise ValidationError(
                        "native physical handoff output generation changed"
                    )
                output_sources.append(StoreSource(path, schema, observed))
            report = read_small_object(output_sources[1].path, 262144)
            if (
                report.get("status") != "pass"
                or report.get("implementation_id")
                != "native-physical-handoff-bridge-v1"
                or report.get("physical_handoff") != str(output_sources[0].path)
                or (
                    mapped_sha256 is not None
                    and report.get("source", {}).get("mapped_sha256") != mapped_sha256
                )
            ):
                raise ValidationError("native physical handoff summary disagrees")
            require_store_sources_unchanged((*leases, *output_sources))
            if packed_value_sink is not None or placement_value_sink is not None:
                from emuflow_tools.physical.xilinx_physical_handoff import open_xilinx_physical_handoff

                packed, placement = open_xilinx_physical_handoff(
                    output_sources[0].path,
                    validated_sha256=report["physical_handoff_sha256"],
                )
                if packed_value_sink is not None:
                    packed_value_sink.clear()
                    packed_value_sink.update(packed)
                if placement_value_sink is not None:
                    placement_value_sink.clear()
                    placement_value_sink.update(placement)
            require_store_sources_unchanged((*leases, *output_sources))
            return report
    except BaseException:
        if generation is not None:
            shutil.rmtree(generation, ignore_errors=True)
        raise


OPENPARF_ATOMIC_BRIDGE_REPORT_SCHEMA = (
    "emuflow.openparf-atomic-physical-bridge-report/v1"
)


def audit_physical_handoff(
    mapped_path,
    handoff_path,
    architecture_path,
    *,
    native_constraints_path=None,
    provider_manifest_path=None,
    constraints_path=None,
):
    """Independently validate the typed physical owner against original inputs."""
    require_native_schema("physical-handoff-audit-request-v1")
    if (native_constraints_path is None) != (provider_manifest_path is None):
        raise ValidationError(
            "physical audit requires both native constraints and provider"
        )
    roles = [
        (mapped_path, "xilinx-mapped-index-v2"),
        (handoff_path, "xilinx-physical-handoff-v1"),
        (architecture_path, "physical-input-document-v1"),
    ]
    flags = 0
    if native_constraints_path is not None:
        flags |= 1
        roles.extend(
            (
                (native_constraints_path, "physical-input-document-v1"),
                (provider_manifest_path, "physical-input-document-v1"),
            )
        )
    if constraints_path is not None:
        flags |= 2
        roles.append((constraints_path, "physical-input-document-v1"))
    sources = tuple(
        bind_store_source(Path(path).resolve(), schema) for path, schema in roles
    )
    with tempfile.TemporaryDirectory(prefix="physical-handoff-audit-") as temporary:
        root = Path(temporary).resolve()
        request = root / "request.bin"
        summary = root / "summary.json"
        operation = root / "operation.json"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QIB", 0x31414850464D5545, 1, flags)
            for source in sources:
                writer.put("3Q4q", *source.generation)
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        executable = resolve_native_executable("emuflow-native")
        command = [
            executable,
            "--report",
            str(operation),
            "physical",
            "audit-handoff",
            *(str(source.path) for source in sources),
            str(request),
            str(summary),
        ]
        with NativeToolProcess(
            executable, command, environment=dict(os.environ), scratch_parent=root
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native physical handoff audit failed: " + completed.stdout
            )
        proof = read_small_object(operation, 262144)
        outputs = proof.get("outputs", [])
        if (
            proof.get("stage") != "physical.audit-handoff"
            or proof.get("status") != "pass"
            or len(outputs) != 1
        ):
            raise ValidationError("native physical handoff audit proof disagrees")
        record = outputs[0]
        generation = tuple(record.get("generation", {}).get(key) for key in _FIELDS)
        if (
            record.get("path") != str(summary)
            or record.get("schema") != "physical-handoff-audit-report-v1"
            or summary.is_symlink()
            or any(type(value) is not int for value in generation)
            or generation != native_file_generation(summary)
        ):
            raise ValidationError("native physical audit output generation changed")
        envelope = read_small_object(summary, 262144)
        if (
            envelope.get("schema") != "emuflow.physical-handoff-audit/v1"
            or envelope.get("status") != "pass"
            or envelope.get("implementation_id") != "native-physical-handoff-audit-v1"
        ):
            raise ValidationError("native physical audit identity disagrees")
        result = envelope.get("validation")
        if not isinstance(result, dict) or set(result) != {
            "packing",
            "placement",
            "source_population_validation",
        }:
            raise ValidationError("native physical audit summary coverage disagrees")
        for kind, schema in (
            ("packing", "emuflow.packed-site-netlist-validation/v1"),
            ("placement", "emuflow.xilinx-placement-validation/v1"),
        ):
            value = result[kind]
            if (
                not isinstance(value, dict)
                or value.get("schema") != schema
                or value.get("status") != "pass"
                or type(value.get("cells")) is not int
                or value["cells"] < 0
                or type(value.get("clusters")) is not int
                or value["clusters"] < 0
            ):
                raise ValidationError("native physical audit summary disagrees")
        require_store_sources_unchanged(
            (
                *sources,
                StoreSource(summary, "physical-handoff-audit-report-v1", generation),
            )
        )
        return result
