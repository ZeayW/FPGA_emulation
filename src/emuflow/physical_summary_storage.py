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
import shutil
from typing import Any, Dict, Iterator, Mapping, Sequence
import uuid

from .errors import ValidationError
from .io import file_sha256, read_json


PHYSICAL_EVIDENCE_REFERENCE_SCHEMA = (
    "emuflow.physical-evidence-reference/v1"
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


def publish_physical_evidence_reference(
    source: Path,
    logical_schema: str,
    output_directory: Path,
    relative_path: Path,
) -> Dict[str, Any]:
    """Publish a relocatable evidence member, preferring a zero-copy link."""

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("physical evidence relative path is unsafe")
    resolved_source = source.resolve()
    if source.is_symlink() or not resolved_source.is_file():
        raise ValidationError("physical evidence artifact is missing")
    target = output_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() != resolved_source:
        temporary = target.with_name(
            f".{target.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            try:
                os.link(resolved_source, temporary)
            except OSError:
                with resolved_source.open("rb") as input_stream:
                    with temporary.open("xb") as output_stream:
                        shutil.copyfileobj(
                            input_stream, output_stream, 1024 * 1024
                        )
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return physical_evidence_reference(
        target, logical_schema, relative_to=output_directory
    )


def _validate_reference(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != (
        PHYSICAL_EVIDENCE_REFERENCE_SCHEMA
    ):
        raise ValidationError("physical evidence reference is invalid")
    if (
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
    return value


def _read_reference(
    value: Mapping[str, Any], base_directory: Path
) -> Dict[str, Any]:
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
        self, references: Mapping[str, Any], base_directory: Path
    ):
        if not references or any(
            not isinstance(fpga, str) or not fpga for fpga in references
        ):
            raise ValidationError("physical evidence FPGA map is invalid")
        for value in references.values():
            _validate_reference(value)
        super().__init__(references)
        self.base_directory = base_directory

    def __getitem__(self, key: str) -> Dict[str, Any]:
        return _read_reference(
            super().__getitem__(key), self.base_directory
        )

    def get(self, key: str, default: Any = None) -> Any:
        return default if key not in self else self[key]

    def items(self) -> Iterator[tuple[str, Dict[str, Any]]]:
        for key in super().keys():
            yield key, self[key]

    def values(self) -> Iterator[Dict[str, Any]]:
        for key in super().keys():
            yield self[key]


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


def read_physical_summary(path: Path) -> Dict[str, Any]:
    """Read a physical summary while keeping large evidence maps lazy."""

    summary = read_json(path)
    for field in _EVIDENCE_FIELDS:
        value = summary.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValidationError(f"physical summary {field} map is invalid")
        referenced = [
            isinstance(item, Mapping)
            and item.get("schema") == PHYSICAL_EVIDENCE_REFERENCE_SCHEMA
            for item in value.values()
        ]
        unknown_references = [
            isinstance(item, Mapping)
            and isinstance(item.get("schema"), str)
            and item["schema"].startswith(
                "emuflow.physical-evidence-reference/"
            )
            and item["schema"] != PHYSICAL_EVIDENCE_REFERENCE_SCHEMA
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
            summary[field] = LazyPhysicalEvidenceMap(value, path.parent)
    return summary
