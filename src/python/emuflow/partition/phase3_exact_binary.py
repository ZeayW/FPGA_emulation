"""Compact binary data plane for Phase 3 Static Exact relations.

The Phase 3 assignment SQLite database remains a small relational control
plane for instance ownership and cut sinks.  The much larger Static Exact
relations are stored here as integer-coded, per-FPGA sequential sections.
Production readers validate only the sections they consume and never build a
SQLite B-tree for tens of millions of semantic rows.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import struct
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from emuflow.common.errors import ValidationError
from emuflow.partition.phase3_exact_limits import STATIC_EXACT_CAPTURE_BIT_ERROR, STATIC_EXACT_CAPTURE_BIT_MAX_BITS, STATIC_EXACT_CAPTURE_BIT_MAX_BYTES


EXACT_BINARY_SCHEMA = "emuflow.phase3-static-exact-binary/v2"
_LEGACY_EXACT_BINARY_SCHEMA = "emuflow.phase3-static-exact-binary/v1"
EXACT_BINARY_ARTIFACT = "assignment.exact.bin"
_MAGIC = b"EMUEXACT"
_VERSION = 2
_SUPPORTED_SCHEMAS = {
    1: _LEGACY_EXACT_BINARY_SCHEMA,
    2: EXACT_BINARY_SCHEMA,
}
_PREAMBLE = struct.Struct("<8sIQQ")
_U32 = struct.Struct("<I")
_SEGMENT = struct.Struct("<IBBII")
_CAPTURE = struct.Struct("<IIB")
_MISSING = 0xFFFFFFFF


def _number(value: Any, prefix: str) -> int:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValidationError(f"Static Exact {prefix} identity is invalid")
    suffix = value[len(prefix):]
    if not suffix.isdigit():
        raise ValidationError(f"Static Exact {prefix} identity is invalid")
    number = int(suffix)
    if number < 0 or number >= _MISSING:
        raise ValidationError(f"Static Exact {prefix} identity is out of range")
    return number


def _identity(prefix: str, number: int) -> str:
    return f"{prefix}{number:06d}"


def _load_manifest(
    path: Path, descriptor: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError("Static Exact binary artifact is missing")
    with path.open("rb") as stream:
        preamble = stream.read(_PREAMBLE.size)
        if len(preamble) != _PREAMBLE.size:
            raise ValidationError("Static Exact binary preamble is truncated")
        magic, version, offset, length = _PREAMBLE.unpack(preamble)
        size = path.stat().st_size
        expected_schema = _SUPPORTED_SCHEMAS.get(version)
        if (
            magic != _MAGIC
            or expected_schema is None
            or offset < _PREAMBLE.size
            or length <= 0
            or offset + length != size
        ):
            raise ValidationError("Static Exact binary preamble is invalid")
        stream.seek(offset)
        encoded = stream.read(length)
    try:
        manifest = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("Static Exact binary manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != expected_schema:
        raise ValidationError("Static Exact binary schema is invalid")
    if descriptor is not None:
        if (
            descriptor.get("artifact") != EXACT_BINARY_ARTIFACT
            or descriptor.get("schema") != expected_schema
            or descriptor.get("bytes") != size
            or descriptor.get("manifest_sha256")
            != hashlib.sha256(encoded).hexdigest()
            or descriptor.get("semantic_contract_schema")
            != manifest.get("semantic_contract_schema")
            or descriptor.get("semantic_contract_sha256")
            != manifest.get("semantic_contract_sha256")
            or descriptor.get("counts") != manifest.get("counts")
            or (
                version == 2
                and descriptor.get("capture_bit_max_bits")
                != STATIC_EXACT_CAPTURE_BIT_MAX_BITS
            )
        ):
            raise ValidationError("Static Exact binary descriptor disagrees")
    if (
        version == 2
        and manifest.get("capture_bit_max_bits")
        != STATIC_EXACT_CAPTURE_BIT_MAX_BITS
    ):
        raise ValidationError("Static Exact binary capture-bit limit is invalid")
    sections = manifest.get("sections")
    parts = manifest.get("parts")
    if not isinstance(sections, dict) or not isinstance(parts, list) or not parts:
        raise ValidationError("Static Exact binary manifest is incomplete")
    if (
        len(parts) > 255
        or not all(isinstance(part, str) and part for part in parts)
        or parts != sorted(parts)
        or len(parts) != len(set(parts))
    ):
        raise ValidationError("Static Exact binary parts are invalid")
    expected_sections = {"nets", "node_sources"}
    expected_sections.update(
        f"{kind}:{part}"
        for part in range(len(parts))
        for kind in ("segments", "captures")
    )
    if set(sections) != expected_sections:
        raise ValidationError("Static Exact binary sections are incomplete")
    occupied: list[tuple[int, int]] = []
    for name, section in sections.items():
        if not isinstance(name, str) or not isinstance(section, dict):
            raise ValidationError("Static Exact binary section is invalid")
        section_offset = section.get("offset")
        section_bytes = section.get("bytes")
        records = section.get("records")
        digest = section.get("sha256")
        if (
            isinstance(section_offset, bool)
            or not isinstance(section_offset, int)
            or isinstance(section_bytes, bool)
            or not isinstance(section_bytes, int)
            or isinstance(records, bool)
            or not isinstance(records, int)
            or section_offset < _PREAMBLE.size
            or section_bytes < 0
            or records < 0
            or section_offset + section_bytes > offset
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValidationError("Static Exact binary section descriptor is invalid")
        if name == "nets":
            minimum_record_bytes = _U32.size + 1
        elif name == "node_sources":
            minimum_record_bytes = _U32.size
        elif name.startswith("segments:"):
            minimum_record_bytes = _SEGMENT.size
        else:
            # Every capture contains the fixed header and one non-empty
            # endpoint string.  V2 architectural records additionally carry
            # a bounded length-prefixed unsigned bit encoding.
            minimum_record_bytes = _CAPTURE.size + _U32.size + 1
        if records > section_bytes // minimum_record_bytes:
            raise ValidationError("Static Exact binary section count is invalid")
        if section_bytes:
            occupied.append((section_offset, section_offset + section_bytes))
    occupied.sort()
    if any(
        end > occupied[index + 1][0]
        for index, (_start, end) in enumerate(occupied[:-1])
    ):
        raise ValidationError("Static Exact binary sections overlap")
    counts = manifest.get("counts")
    count_names = (
        "cut_nodes",
        "source_segments",
        "logic_segments",
        "consumer_segments",
        "capture_requirements",
    )
    if not isinstance(counts, dict) or any(
        isinstance(counts.get(name), bool)
        or not isinstance(counts.get(name), int)
        or counts[name] < 0
        for name in count_names
    ):
        raise ValidationError("Static Exact binary population counts are invalid")
    segment_records = sum(
        sections[f"segments:{part}"]["records"]
        for part in range(len(parts))
    )
    capture_records = sum(
        sections[f"captures:{part}"]["records"]
        for part in range(len(parts))
    )
    node_section = sections["node_sources"]
    maximum_source_records = (
        node_section["bytes"]
        - node_section["records"] * _U32.size
    ) // _U32.size
    if (
        counts["cut_nodes"] != sections["nets"]["records"]
        or counts["cut_nodes"] != node_section["records"]
        or counts["source_segments"] > maximum_source_records
        or counts["logic_segments"] != segment_records
        or counts["consumer_segments"] > counts["logic_segments"]
        or counts["capture_requirements"] != capture_records
    ):
        raise ValidationError("Static Exact binary population counts disagree")
    # Runtime metadata derived from the authenticated preamble.  It is never
    # serialized into the manifest or included in semantic digests.
    manifest["_format_version"] = version
    return manifest


class _SectionReader:
    def __init__(self, path: Path, section: Mapping[str, Any]):
        self.stream = path.open("rb")
        self.remaining = int(section["bytes"])
        self.expected_digest = str(section["sha256"])
        self.digest = hashlib.sha256()
        self.stream.seek(int(section["offset"]))
        self.finished = False

    def read(self, amount: int) -> bytes:
        if amount < 0 or amount > self.remaining:
            raise ValidationError("Static Exact binary record exceeds its section")
        value = self.stream.read(amount)
        if len(value) != amount:
            raise ValidationError("Static Exact binary section is truncated")
        self.remaining -= amount
        self.digest.update(value)
        return value

    def string(self) -> str:
        length = _U32.unpack(self.read(_U32.size))[0]
        try:
            value = self.read(length).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("Static Exact binary string is invalid") from error
        if not value:
            raise ValidationError("Static Exact binary string is empty")
        return value

    def finish(self) -> None:
        if self.remaining != 0:
            raise ValidationError("Static Exact binary section has trailing bytes")
        if self.digest.hexdigest() != self.expected_digest:
            raise ValidationError("Static Exact binary section digest disagrees")
        self.finished = True
        self.stream.close()

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


def _section(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = manifest["sections"].get(name)
    if not isinstance(section, Mapping):
        raise ValidationError(f"Static Exact binary section {name!r} is missing")
    return section


def _read_nets(path: Path, manifest: Mapping[str, Any]) -> list[str]:
    section = _section(manifest, "nets")
    reader = _SectionReader(path, section)
    try:
        values = [reader.string() for _ in range(int(section["records"]))]
        reader.finish()
    finally:
        reader.close()
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValidationError("Static Exact binary cut nets are invalid")
    return values


def iter_phase3_exact_node_sources(
    path: Path, descriptor: Mapping[str, Any]
) -> Iterator[tuple[str, list[int]]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    section = _section(manifest, "node_sources")
    if int(section["records"]) != len(nets):
        raise ValidationError("Static Exact binary node coverage disagrees")
    reader = _SectionReader(path, section)
    try:
        for net in nets:
            count = _U32.unpack(reader.read(_U32.size))[0]
            values = [
                _U32.unpack(reader.read(_U32.size))[0] for _ in range(count)
            ]
            yield net, values
        reader.finish()
    finally:
        reader.close()


def _part_index(manifest: Mapping[str, Any], fpga: str) -> int:
    try:
        return list(manifest["parts"]).index(fpga)
    except ValueError as error:
        raise ValidationError(f"Static Exact binary omits FPGA {fpga!r}") from error


def _iter_segments(
    path: Path,
    manifest: Mapping[str, Any],
    part: int,
    nets: Sequence[str],
) -> Iterator[dict[str, Any]]:
    fpga = manifest["parts"][part]
    section = _section(manifest, f"segments:{part}")
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            number, kind, flags, source, target = _SEGMENT.unpack(
                reader.read(_SEGMENT.size)
            )
            if kind not in {0, 1, 2} or flags & ~1:
                raise ValidationError("Static Exact binary segment flags are invalid")
            value: dict[str, Any] = {
                "id": _identity("segment", number),
                "kind": ("launch_to_tx", "rx_to_tx", "rx_to_capture")[kind],
                "fpga": fpga,
            }
            if kind == 0:
                if source != _MISSING or target >= len(nets):
                    raise ValidationError(
                        "Static Exact binary launch segment is invalid"
                    )
                value["sink_cut_net"] = nets[target]
                if flags:
                    value["source_semantics"] = "configuration-stable-constant"
            elif kind == 1:
                if flags or source >= len(nets) or target >= len(nets):
                    raise ValidationError(
                        "Static Exact binary dependency segment is invalid"
                    )
                value["source_cut_net"] = nets[source]
                value["sink_cut_net"] = nets[target]
            else:
                if flags or source >= len(nets) or target == _MISSING:
                    raise ValidationError(
                        "Static Exact binary capture segment is invalid"
                    )
                value["source_cut_net"] = nets[source]
                value["capture_requirement"] = _identity("capture", target)
            yield value
        reader.finish()
    finally:
        reader.close()


def _iter_captures(
    path: Path,
    manifest: Mapping[str, Any],
    part: int,
    nets: Sequence[str],
) -> Iterator[dict[str, Any]]:
    fpga = manifest["parts"][part]
    section = _section(manifest, f"captures:{part}")
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            number, cut_net, kind = _CAPTURE.unpack(reader.read(_CAPTURE.size))
            if cut_net >= len(nets) or kind not in {0, 1}:
                raise ValidationError("Static Exact binary capture record is invalid")
            value: dict[str, Any] = {
                "id": _identity("capture", number),
                "cut_net": nets[cut_net],
                "fpga": fpga,
                "kind": "top-output" if kind == 0 else "architectural-state",
                "endpoint": reader.string(),
            }
            if kind == 1:
                value["port"] = reader.string()
                if manifest.get("_format_version") == 1:
                    value["bit"] = _U32.unpack(reader.read(_U32.size))[0]
                else:
                    length = _U32.unpack(reader.read(_U32.size))[0]
                    if length > STATIC_EXACT_CAPTURE_BIT_MAX_BYTES:
                        raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
                    if length == 0 or length > reader.remaining:
                        raise ValidationError(
                            "Static Exact binary capture bit is invalid"
                        )
                    encoded_bit = reader.read(length)
                    if length > 1 and encoded_bit[0] == 0:
                        raise ValidationError(
                            "Static Exact binary capture bit is not canonical"
                        )
                    value["bit"] = int.from_bytes(encoded_bit, "big")
            yield value
        reader.finish()
    finally:
        reader.close()


def iter_phase3_exact_segments(
    path: Path,
    descriptor: Mapping[str, Any],
    fpga: str | None = None,
) -> Iterator[dict[str, Any]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    if fpga is not None:
        yield from _iter_segments(path, manifest, _part_index(manifest, fpga), nets)
        return
    streams = [
        _iter_segments(path, manifest, part, nets)
        for part in range(len(manifest["parts"]))
    ]
    yield from heapq.merge(
        *streams, key=lambda item: _number(item["id"], "segment")
    )


def iter_phase3_exact_captures(
    path: Path,
    descriptor: Mapping[str, Any],
    fpga: str | None = None,
) -> Iterator[dict[str, Any]]:
    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    if fpga is not None:
        yield from _iter_captures(path, manifest, _part_index(manifest, fpga), nets)
        return
    streams = [
        _iter_captures(path, manifest, part, nets)
        for part in range(len(manifest["parts"]))
    ]
    yield from heapq.merge(
        *streams, key=lambda item: _number(item["id"], "capture")
    )


def phase3_exact_binary_counts(
    path: Path, descriptor: Mapping[str, Any]
) -> dict[str, int]:
    manifest = _load_manifest(path, descriptor)
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValidationError("Static Exact binary counts are invalid")
    return {key: int(value) for key, value in counts.items()}


def validate_phase3_exact_binary(path: Path, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every physical section in the shared core, without host graphs."""
    from emuflow.partition.phase3_exact_native import validate
    return validate(path, descriptor)
