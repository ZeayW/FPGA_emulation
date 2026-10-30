"""Bounded source host for independent native route certificate validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import (
    native_file_generation,
    require_native_schema,
    resolve_native_executable,
)
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow.storage.store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
)


def _write_route_request(path, sources, configuration):
    writer = CheckedRequestWriter(path)
    try:
        writer.put("QII", 0x31565250464D5545, 1, len(sources))
        for role, source in sources:
            writer.text(role)
            writer.text(str(source.path))
            writer.text(source.schema)
            writer.put("3Q4q", *source.generation)
        writer.text(
            json.dumps(
                configuration, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        )
        writer.finish()
    finally:
        writer.close()


def validate_native_route(
    path,
    *,
    rwroute_input_path=None,
    mapped_path=None,
    packed_path=None,
    placement_path=None,
    mapped_value=None,
    packed_value=None,
    source_sha256=None,
    virtual_io_contract=None,
    _route_source=None,
):
    from emuflow.timing.rapidwright_contract import RAPIDWRIGHT_TIMING_CONTRACT

    require_native_schema("xilinx-route-validation-request-v1")
    route = bind_store_source(Path(path).resolve(), "xilinx-route-manifest-v2")
    if _route_source is not None:
        if _route_source != route:
            raise ValidationError(
                "route validation source belongs to another artifact generation"
            )
        require_store_sources_unchanged((_route_source,))
    # Borrowed readers remain leased; native code consumes the original owners.
    if mapped_value is not None and hasattr(mapped_value, "require_source_unchanged"):
        if (
            mapped_path is None
            or Path(mapped_value.path).resolve() != Path(mapped_path).resolve()
        ):
            raise ValidationError("route mapped owner belongs to another artifact")
        mapped_value.require_source_unchanged()
    borrowed = ()
    if packed_value is not None:
        relation = packed_value.get("clusters")
        bind = getattr(relation, "bound_store_sources", None)
        if callable(bind):
            borrowed = bind()
            if packed_path is None or not any(
                source.path == Path(packed_path).resolve() for source in borrowed
            ):
                raise ValidationError(
                    "route physical owner belongs to another artifact"
                )
            require_store_sources_unchanged(borrowed)
    sources = [
        ("route", route),
        (
            "timing",
            bind_store_source(
                RAPIDWRIGHT_TIMING_CONTRACT.resolve(), "physical-input-document-v1"
            ),
        ),
    ]
    for role, value in (
        ("mapped", mapped_path),
        ("packed", packed_path),
        ("placement", placement_path),
        ("input", rwroute_input_path),
    ):
        if value is not None:
            sources.append(
                (
                    role,
                    bind_store_source(
                        Path(value).resolve(), "physical-input-document-v1"
                    ),
                )
            )
    configuration = {
        "schema": "emuflow.route-validation-request/v1",
        "virtual_io": virtual_io_contract,
        "virtual_io_sha256": hashlib.sha256(
            json.dumps(
                virtual_io_contract, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if virtual_io_contract is not None
        else None,
    }
    with tempfile.TemporaryDirectory(prefix="route-certificate-") as temporary:
        root = Path(temporary).resolve()
        request, summary, operation = (
            root / "request.bin",
            root / "summary.json",
            root / "operation.json",
        )
        _write_route_request(request, sources, configuration)
        leases = (*tuple(source for _, source in sources), *borrowed)
        require_store_sources_unchanged(leases)
        executable = resolve_native_executable("emuflow-native")
        command = [
            executable,
            "--report",
            str(operation),
            "physical",
            "validate-route",
            str(request),
            str(summary),
        ]
        with NativeToolProcess(
            executable, command, environment=dict(os.environ), scratch_parent=root
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native route certificate validation failed: " + completed.stdout
            )
        proof = read_small_object(operation, 262144)
        outputs = proof.get("outputs", [])
        if (
            proof.get("stage") != "physical.validate-route"
            or proof.get("status") != "pass"
            or len(outputs) != 1
        ):
            raise ValidationError("native route validation proof disagrees")
        row = outputs[0]
        fields = (
            "device",
            "inode",
            "bytes",
            "mtime_seconds",
            "mtime_nanoseconds",
            "ctime_seconds",
            "ctime_nanoseconds",
        )
        generation = tuple(row.get("generation", {}).get(key) for key in fields)
        if (
            row.get("path") != str(summary)
            or row.get("schema") != "xilinx-route-validation-report-v1"
            or any(type(value) is not int for value in generation)
            or generation != native_file_generation(summary)
        ):
            raise ValidationError("native route validation output generation changed")
        report = read_small_object(summary, 262144)
        if (
            report.get("schema") != "emuflow.xilinx-route-validation/v1"
            or report.get("status") != "pass"
            or report.get("implementation_id") != "native-xilinx-route-validation-v1"
        ):
            raise ValidationError("native route validation summary disagrees")
        require_store_sources_unchanged(
            (
                *leases,
                StoreSource(summary, "xilinx-route-validation-report-v1", generation),
            )
        )
        return report
