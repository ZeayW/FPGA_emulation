"""Small source-reference host for the native OpenPARF input producer."""

from __future__ import annotations

from emuflow.common.paths import DATA_ROOT
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import write_json
from emuflow.common.native_tools import (
    native_file_generation,
    resolve_native_executable,
)
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow.storage.store_publication import (
    bind_store_source,
    require_store_sources_unchanged,
    validate_store_destination,
)


def _run(operation, output, sources, *, top="", allowed_slrs=None):
    output = Path(output).absolute()
    bound = tuple(
        bind_store_source(Path(path).resolve(), schema) for _, path, schema in sources
    )
    validate_store_destination(output, bound)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".openparf-request-", dir=output.parent
    ) as directory:
        root = Path(directory)
        request, report = root / "request.json", root / "operation.json"
        write_json(
            request,
            {
                "schema": "emuflow.openparf-input-request/v1",
                "top": top or "",
                "allowed_slrs": (
                    list(allowed_slrs) if allowed_slrs is not None else None
                ),
                "sources": [
                    {
                        "role": role,
                        "path": str(source.path),
                        "schema": source.schema,
                        "generation": list(source.generation),
                    }
                    for (role, _, _), source in zip(sources, bound)
                ],
            },
            compact=True,
        )
        executable = resolve_native_executable("emuflow-native")
        command = [
            executable,
            "--report",
            str(report),
            "physical",
            operation,
            str(request),
            str(output),
        ]
        with NativeToolProcess(
            executable, command, environment=dict(os.environ), scratch_parent=root
        ) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError(
                "native OpenPARF input preparation failed: " + result.stdout
            )
        proof = read_small_object(report, 262144)
        if (
            proof.get("stage") != "physical." + operation
            or proof.get("status") != "pass"
        ):
            raise ValidationError("native OpenPARF input proof disagrees")
        require_store_sources_unchanged(bound)
        artifact = (
            output / "manifest.json"
            if operation == "prepare-openparf-input"
            else output
        )
        rows = proof.get("outputs", [])
        fields = (
            "device",
            "inode",
            "bytes",
            "mtime_seconds",
            "mtime_nanoseconds",
            "ctime_seconds",
            "ctime_nanoseconds",
        )
        if (
            len(rows) != (2 if operation == "build-openparf-source" else 1)
            or rows[0].get("path") != str(artifact)
            or tuple(rows[0].get("generation", {}).get(k) for k in fields)
            != native_file_generation(artifact)
        ):
            raise ValidationError("native OpenPARF publication generation changed")
        return artifact, proof


def build_atomic_source(
    mapped_path, output_path, *, top=None, mapped_value=None, mapped_sha256=None
):
    from emuflow_tools.physical.xilinx_netlist import is_xilinx_mapped_index
    from emuflow_tools.physical.openparf.xilinx_openparf_source_storage import XilinxOpenparfAtomicSourceView

    schema = (
        "xilinx-mapped-index-v2"
        if is_xilinx_mapped_index(mapped_path)
        else "xilinx-mapped-json-v1"
    )
    if mapped_value is not None:
        require = getattr(mapped_value, "require_source_unchanged", None)
        if callable(require):
            require()
    selector, proof = _run(
        "build-openparf-source", output_path, [("mapped", mapped_path, schema)], top=top
    )
    fields = (
        "device",
        "inode",
        "bytes",
        "mtime_seconds",
        "mtime_nanoseconds",
        "ctime_seconds",
        "ctime_nanoseconds",
    )
    store = proof["outputs"][1]
    if tuple(
        store.get("generation", {}).get(k) for k in fields
    ) != native_file_generation(store["path"]):
        raise ValidationError("native OpenPARF source generation changed")
    return XilinxOpenparfAtomicSourceView._from_native_publication(
        selector,
        Path(mapped_path),
        store,
        mapped_value=mapped_value,
        mapped_sha256=mapped_sha256,
        receipt=proof,
    )


def prepare_atomic_input(
    mapped_path,
    packed_path,
    architecture_path,
    output_dir,
    *,
    top=None,
    native_constraints_path=None,
    provider_manifest_path=None,
    mapped_value=None,
    packed_value=None,
    architecture=None,
    allowed_slrs=None,
):
    sources, effective_top = atomic_input_sources(mapped_path, packed_path, architecture_path,
        top=top, native_constraints_path=native_constraints_path, provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value, packed_value=packed_value)
    manifest, proof = _run(
        "prepare-openparf-input",
        output_dir,
        sources,
        top=effective_top,
        allowed_slrs=allowed_slrs,
    )
    value = read_small_object(manifest, 8 * 1024 * 1024)
    fields = (
        "device",
        "inode",
        "bytes",
        "mtime_seconds",
        "mtime_nanoseconds",
        "ctime_seconds",
        "ctime_nanoseconds",
    )
    if native_file_generation(manifest) != tuple(
        proof["outputs"][0]["generation"][k] for k in fields
    ):
        raise ValidationError("native OpenPARF manifest generation changed")
    return value


def atomic_input_sources(mapped_path, packed_path, architecture_path, *, top=None,
                         native_constraints_path=None, provider_manifest_path=None,
                         mapped_value=None, packed_value=None):
    from emuflow_tools.physical.xilinx_netlist import IndexedXilinxMappedNetlist, is_xilinx_mapped_index

    if not isinstance(mapped_value, IndexedXilinxMappedNetlist):
        raise ValidationError(
            "OpenPARF Bookshelf export requires a current mapped-index owner"
        )
    mapped_value.require_source_unchanged()
    if (native_constraints_path is None) != (provider_manifest_path is None):
        raise ValidationError(
            "typed hardblock route requires both native constraints and provider manifest"
        )
    identity_schema = (
        "xilinx-mapped-index-v2"
        if is_xilinx_mapped_index(mapped_path)
        else "xilinx-mapped-json-v1"
    )
    policy = DATA_ROOT / "openparf/input-policy.json"
    sources = [
        ("mapped", mapped_value.path, "xilinx-mapped-index-v2"),
        ("mapped_identity", mapped_path, identity_schema),
        ("packed", packed_path, "physical-input-document-v1"),
        ("architecture", architecture_path, "physical-input-document-v1"),
        ("policy", policy, "physical-input-document-v1"),
    ]
    if native_constraints_path is not None:
        sources += [
            ("native", native_constraints_path, "physical-input-document-v1"),
            ("provider", provider_manifest_path, "physical-input-document-v1"),
        ]
    for view in (packed_value,):
        require = getattr(view, "require_sources_unchanged", None)
        if callable(require):
            require()
    return sources, top or mapped_value.top
