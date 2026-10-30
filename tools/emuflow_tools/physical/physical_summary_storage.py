"""Lazy references for graph-sized physical evidence databases.

The physical summary is a bounded control artifact. Endpoint/path evidence
remains owned by the per-FPGA producer and is referenced by an immutable file
seal instead of being serialized into the summary a second time.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Sequence

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow_tools.document_io import read_json


PHYSICAL_EVIDENCE_REFERENCE_SCHEMA = (
    "emuflow.physical-evidence-reference/v1"
)
PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA = (
    "emuflow.physical-evidence-reference/v2"
)
_EVIDENCE_FIELDS = {
    "boundary_identities",
    "boundary_timing",
    "logic_segment_timing",
    "local_path_timing",
}


class _HashingRawReader(io.RawIOBase):
    """Update a digest while a JSON decoder consumes one binary stream."""

    def __init__(self, stream: Any):
        super().__init__()
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = self.stream.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.bytes_read += count
        return count


def _read_sealed_json(path: Path) -> tuple[Dict[str, Any], int, str]:
    with path.open("rb") as binary_stream:
        reader = _HashingRawReader(binary_stream)
        with io.TextIOWrapper(
            io.BufferedReader(reader), encoding="utf-8"
        ) as text_stream:
            try:
                document = json.load(text_stream)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    "physical evidence JSON is invalid"
                ) from error
    if not isinstance(document, dict):
        raise ValidationError("physical evidence JSON root is invalid")
    return document, reader.bytes_read, reader.digest.hexdigest()


def physical_evidence_reference(
    path: Path, logical_schema: str, *, relative_to: Path
) -> Dict[str, Any]:
    """Seal one already-published per-FPGA evidence artifact."""

    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ValidationError("physical evidence artifact is missing")
    if not isinstance(logical_schema, str) or not logical_schema:
        raise ValidationError("physical evidence logical schema is invalid")
    stat = resolved.stat()
    relative_path = Path(os.path.relpath(resolved, relative_to.resolve()))
    reference = {
        "schema": PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
        "logical_schema": logical_schema,
        "path": str(relative_path),
        "bytes": stat.st_size,
        "sha256": file_sha256(resolved),
    }
    _validate_reference(reference)
    return reference






def publish_logic_segment_timing_store(
    source: Path,
    output_directory: Path,
    relative_path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Publish an existing typed selector and its immutable generation.

    Production producers write the typed owner directly.  This assembly step
    only relocates the bounded selector plus the content-addressed generation;
    it never decodes or retains a graph-sized JSON payload.
    """

    from emuflow_tools.timing.logic_segment_timing_storage import publish_logic_segment_timing_selector

    return publish_logic_segment_timing_selector(
        source, output_directory, relative_path
    )

def publish_boundary_identity_store(
    source: Path,
    output_directory: Path,
    relative_path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Relocate a typed boundary selector and generation without expansion."""

    from emuflow_tools.timing.boundary_identity_storage import publish_boundary_identity_selector

    return publish_boundary_identity_selector(
        source, output_directory, relative_path
    )


def publish_boundary_timing_store(
    source: Path,
    output_directory: Path,
    relative_path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Relocate a typed boundary-timing selector without expansion."""

    from emuflow_tools.timing.boundary_timing_storage import publish_boundary_timing_selector

    return publish_boundary_timing_selector(
        source, output_directory, relative_path
    )


def publish_local_path_timing_store(
    source: Path,
    output_directory: Path,
    relative_path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Relocate a typed local-path selector without expanding its rows."""

    from emuflow_tools.timing.local_path_timing_storage import publish_local_path_timing_selector

    return publish_local_path_timing_selector(
        source, output_directory, relative_path
    )


def _validate_reference(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") not in {
        PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
        PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA,
    }:
        raise ValidationError("physical evidence reference is invalid")
    expected_keys = {"schema", "logical_schema", "path", "bytes", "sha256"}
    if (
        set(value) != expected_keys
        or
        not isinstance(value.get("logical_schema"), str)
        or not value["logical_schema"]
        or not isinstance(value.get("path"), str)
        or not value["path"]
        or isinstance(value.get("bytes"), bool)
        or not isinstance(value.get("bytes"), int)
        or value["bytes"] < 0
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["sha256"]
        )
    ):
        raise ValidationError("physical evidence reference is malformed")
    stored_path = Path(value["path"])
    if stored_path.is_absolute() or ".." in stored_path.parts:
        raise ValidationError("physical evidence reference path is unsafe")
    if value["schema"] == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA:
        if value.get("logical_schema") not in {
            "emuflow.logic-segment-timing/v1",
            "emuflow.boundary-identity/v1",
            "emuflow.boundary-timing/v1",
            "emuflow.local-path-timing/v1",
        }:
            raise ValidationError(
                "physical indexed evidence reference is malformed"
            )
    return value


def _read_reference(
    value: Mapping[str, Any], base_directory: Path, *,
    expected_design: Any = None, expected_platform: Any = None,
    expected_fpga: Any = None,
    expected_system_routes_sha256: Any = None,
    expected_tdm_schedule_sha256: Any = None,
    expected_semantic_contract_sha256: Any = None,
) -> Mapping[str, Any]:
    reference = _validate_reference(value)
    stored_path = Path(reference["path"])
    path = base_directory / stored_path
    resolved_base = base_directory.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_base)
    except ValueError as error:
        raise ValidationError(
            "physical evidence referenced path escapes its summary"
        ) from error
    if path.is_symlink() or not resolved_path.is_file():
        raise ValidationError("physical evidence referenced file is missing")
    stat = resolved_path.stat()
    if stat.st_size != reference["bytes"]:
        raise ValidationError("physical evidence byte count disagrees")
    if reference["schema"] == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA:
        if reference["logical_schema"] == "emuflow.local-path-timing/v1":
            from emuflow_tools.timing.local_path_timing_storage import read_local_path_timing_storage

            return read_local_path_timing_storage(
                resolved_path,
                expected_design=expected_design,
                expected_fpga=expected_fpga,
                expected_selector_bytes=reference["bytes"],
                expected_selector_sha256=reference["sha256"],
            )
        if reference["logical_schema"] == "emuflow.boundary-timing/v1":
            from emuflow_tools.timing.boundary_timing_storage import read_boundary_timing_storage

            return read_boundary_timing_storage(
                resolved_path,
                expected_design=expected_design,
                expected_platform=expected_platform,
                expected_fpga=expected_fpga,
                expected_selector_bytes=reference["bytes"],
                expected_selector_sha256=reference["sha256"],
            )
        if reference["logical_schema"] == "emuflow.boundary-identity/v1":
            from emuflow_tools.timing.boundary_identity_storage import read_boundary_identity_storage

            result = read_boundary_identity_storage(resolved_path)
            if (
                (expected_design is not None and result.get("design") != expected_design)
                or (
                    expected_platform is not None
                    and result.get("platform") != expected_platform
                )
                or (expected_fpga is not None and result.get("fpga") != expected_fpga)
                or file_sha256(resolved_path) != reference["sha256"]
            ):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise ValidationError(
                    "boundary identity indexed evidence source disagrees"
                )
            return result
        from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage

        return read_logic_segment_timing_storage(
            resolved_path,
            expected_design=expected_design,
            expected_platform=expected_platform,
            expected_fpga=expected_fpga,
            expected_system_routes_sha256=expected_system_routes_sha256,
            expected_tdm_schedule_sha256=expected_tdm_schedule_sha256,
            expected_semantic_contract_sha256=(
                expected_semantic_contract_sha256
            ),
            expected_selector_bytes=reference["bytes"],
            expected_selector_sha256=reference["sha256"],
        )
    document, byte_count, digest = _read_sealed_json(resolved_path)
    if byte_count != reference["bytes"]:
        raise ValidationError("physical evidence byte count disagrees")
    if digest != reference["sha256"]:
        raise ValidationError("physical evidence digest disagrees")
    if document.get("schema") != reference["logical_schema"]:
        raise ValidationError("physical evidence logical schema disagrees")
    return document


class LazyPhysicalEvidenceMap(dict[str, Mapping[str, Any]]):
    """Dictionary-compatible per-FPGA map that resolves one file at a time."""

    def __init__(
        self, references: Mapping[str, Any], base_directory: Path, *,
        expected_design: Any = None, expected_platform: Any = None,
        expected_system_routes_sha256: Any = None,
        expected_tdm_schedule_sha256: Any = None,
        expected_semantic_contract_sha256: Any = None,
    ):
        if not references or any(
            not isinstance(fpga, str) or not fpga for fpga in references
        ):
            raise ValidationError("physical evidence FPGA map is invalid")
        for value in references.values():
            _validate_reference(value)
        super().__init__(references)
        self.base_directory = base_directory
        self._indexed_cache: Dict[str, Mapping[str, Any]] = {}
        self.expected_design = expected_design
        self.expected_platform = expected_platform
        self.expected_system_routes_sha256 = expected_system_routes_sha256
        self.expected_tdm_schedule_sha256 = expected_tdm_schedule_sha256
        self.expected_semantic_contract_sha256 = (
            expected_semantic_contract_sha256
        )
        self._closed = False

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        if self._closed:
            raise ValidationError("physical evidence map is closed")
        reference = super().__getitem__(key)
        if reference.get("schema") == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA:
            cached = self._indexed_cache.get(key)
            if cached is None:
                cached = _read_reference(
                    reference,
                    self.base_directory,
                    expected_design=self.expected_design,
                    expected_platform=self.expected_platform,
                    expected_fpga=key,
                    expected_system_routes_sha256=(
                        self.expected_system_routes_sha256
                    ),
                    expected_tdm_schedule_sha256=(
                        self.expected_tdm_schedule_sha256
                    ),
                    expected_semantic_contract_sha256=(
                        self.expected_semantic_contract_sha256
                    ),
                )
                source = getattr(cached, "selector", {}).get("source", {})
                if (
                    source.get("semantic_contract_sha256") is not None
                    and self.expected_semantic_contract_sha256 is None
                ):
                    cached.close()  # type: ignore[attr-defined]
                    raise ValidationError(
                        "physical summary lacks the authoritative semantic seal"
                    )
                self._indexed_cache[key] = cached
            return cached
        return _read_reference(reference, self.base_directory)

    def get(self, key: str, default: Any = None) -> Any:
        return default if key not in self else self[key]

    def items(self) -> Iterator[tuple[str, Mapping[str, Any]]]:
        for key in super().keys():
            yield key, self[key]

    def values(self) -> Iterator[Mapping[str, Any]]:
        for key in super().keys():
            yield self[key]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        collection = getattr(self, "_logic_collection_cache", None)
        collection_view_ids = {
            id(value) for value in getattr(collection, "views", ())
        }
        if collection is not None:
            collection.close()
        for value in self._indexed_cache.values():
            if id(value) in collection_view_ids:
                continue
            close = getattr(value, "close", None)
            if callable(close):
                close()
        self._indexed_cache.clear()
        self._logic_collection_cache = None

    def __enter__(self) -> "LazyPhysicalEvidenceMap":
        if self._closed:
            raise ValidationError("physical evidence map is closed")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def canonical_physical_summary_sha256(summary: Mapping[str, Any]) -> str:
    """Hash logical summary content without materializing every FPGA database."""

    digest = hashlib.sha256()

    def update(value: Any) -> None:
        if isinstance(value, Mapping):
            digest.update(b"{")
            for index, key in enumerate(sorted(value)):
                if not isinstance(key, str):
                    raise ValidationError(
                        "physical summary canonical mapping key is invalid"
                    )
                if index:
                    digest.update(b",")
                digest.update(
                    json.dumps(key, ensure_ascii=True).encode("utf-8")
                )
                digest.update(b":")
                update(value[key])
            digest.update(b"}")
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            digest.update(b"[")
            for index, item in enumerate(value):
                if index:
                    digest.update(b",")
                update(item)
            digest.update(b"]")
            return
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "physical summary contains a non-canonical value"
            ) from error
        digest.update(encoded.encode("utf-8"))

    update(summary)
    return digest.hexdigest()


class PhysicalSummaryView(dict[str, Any]):
    """Summary owner that deterministically releases all evidence leases."""

    def close(self) -> None:
        for value in self.values():
            close = getattr(value, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "PhysicalSummaryView":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

def read_physical_summary(path: Path) -> PhysicalSummaryView:
    """Read a physical summary while keeping large evidence maps lazy."""

    summary = PhysicalSummaryView(read_json(path))
    try:
        for field in _EVIDENCE_FIELDS:
            value = summary.get(field)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ValidationError(f"physical summary {field} map is invalid")
            referenced = [
                isinstance(item, Mapping)
                and item.get("schema") in {
                    PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
                    PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA,
                }
                for item in value.values()
            ]
            unknown_references = [
                isinstance(item, Mapping)
                and isinstance(item.get("schema"), str)
                and item["schema"].startswith(
                    "emuflow.physical-evidence-reference/"
                )
                and item["schema"] not in {
                    PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
                    PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA,
                }
                for item in value.values()
            ]
            if any(unknown_references):
                raise ValidationError(
                    f"physical summary {field} reference schema is unsupported"
                )
            if any(referenced):
                if not all(referenced):
                    raise ValidationError(
                        f"physical summary {field} mixes inline and referenced evidence"
                    )
                for item in value.values():
                    _validate_reference(item)
                if field == "logic_segment_timing":
                    indexed = [
                        item.get("schema")
                        == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA
                        for item in value.values()
                    ]
                    if any(indexed) and not all(indexed):
                        raise ValidationError(
                            "physical summary logic segment timing mixes indexed "
                            "and legacy evidence"
                        )
                    if not any(indexed):
                        raise ValidationError(
                            "logic segment timing requires current indexed evidence"
                        )
                    if any(indexed) and any(
                        not isinstance(summary.get(key), str) or not summary[key]
                        for key in (
                            "design", "platform",
                            "logic_segment_system_routes_sha256",
                            "logic_segment_tdm_schedule_sha256",
                        )
                    ):
                        raise ValidationError(
                            "indexed logic timing requires physical summary identity"
                        )
                summary[field] = LazyPhysicalEvidenceMap(
                    value,
                    path.parent,
                    expected_design=summary.get("design"),
                    expected_platform=summary.get("platform"),
                    expected_system_routes_sha256=summary.get(
                        "logic_segment_system_routes_sha256"
                    ) if field == "logic_segment_timing" else None,
                    expected_tdm_schedule_sha256=summary.get(
                        "logic_segment_tdm_schedule_sha256"
                    ) if field == "logic_segment_timing" else None,
                    expected_semantic_contract_sha256=summary.get(
                        "logic_segment_semantic_contract_sha256"
                    ) if field == "logic_segment_timing" else None,
                )
            elif field == "logic_segment_timing":
                raise ValidationError(
                    "logic segment timing requires current indexed evidence"
                )
        return summary
    except BaseException:
        summary.close()
        raise
