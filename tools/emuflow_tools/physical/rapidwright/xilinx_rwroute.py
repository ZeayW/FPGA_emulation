"""Compact RWRoute adapter and independent route-certificate checker."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256


XILINX_ROUTE_NET_PAYLOAD_FORMAT = "jsonl-object/v1"




def _sha256(path: Path) -> str:
    return file_sha256(path)


def _route_payload_path(
    manifest_path: Path,
    descriptor: object,
    *,
    field: str,
    expected_format: str,
) -> tuple[Path, str, int]:
    if not isinstance(descriptor, Mapping):
        raise ValidationError(f"XilinxRouteDB payloads.{field} is invalid")
    relative = descriptor.get("path")
    digest = descriptor.get("sha256")
    records = descriptor.get("records")
    if (
        descriptor.get("format") != expected_format
        or not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(records, bool)
        or not isinstance(records, int)
        or records < 0
    ):
        raise ValidationError(f"XilinxRouteDB payloads.{field} is invalid")
    path = manifest_path.parent / relative
    if not path.is_file():
        raise ValidationError(f"XilinxRouteDB payloads.{field} is missing")
    return path, digest, records


def export_rwroute_input(
    mapped_path: Path,
    physical_handoff_path: Path,
    output_path: Path,
    *,
    source_sha256: Optional[Mapping[str, str]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resource_limits: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """Export only from the two authoritative indexed stores.

    Header/ownership projection and record generation belong to the shared
    native core. This entry has no JSON or Python object exporter.
    """

    from emuflow.common.native_tools import native_file_generation
    from emuflow_tools.physical.rapidwright.rwroute_native import export_native_rwroute

    mapped_path, physical_handoff_path = Path(mapped_path), Path(physical_handoff_path)
    inputs = (mapped_path, physical_handoff_path)
    generations = tuple(native_file_generation(path) for path in inputs)
    for path in inputs:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ValidationError(
                    "RWRoute export requires a mapped SQLite index and one typed "
                    "physical handoff; JSON export is retired"
                )
    if source_sha256 is None:
        physical_digest = _sha256(physical_handoff_path)
        digests = {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": physical_digest,
            "placement_sha256": physical_digest,
        }
    else:
        digests = dict(source_sha256)
    if set(digests) != {
        "mapped_sha256", "packed_sha256", "placement_sha256"
    } or any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in digests.values()
    ):
        raise ValidationError("RWRoute source digests are invalid")
    if tuple(native_file_generation(path) for path in inputs) != generations:
        raise ValidationError("RWRoute export source generation changed")
    return export_native_rwroute(
        mapped_path, physical_handoff_path, output_path,
        digests=digests, virtual_io_contract=virtual_io_contract,
        resource_limits=resource_limits,
    )


def run_rwroute(
    input_path: Path, output_path: Path, *, rapidwright_jar: Path, java: Path,
    java_source: Path, device_data_root: Path, timing_data_dir: Path,
) -> Dict[str, Any]:
    """Run the native route owner on an already exported input."""
    from emuflow_tools.physical.rapidwright.route_stage import run_native_rwroute_stage
    return run_native_rwroute_stage(input_path, output_path,
        rapidwright_jar=rapidwright_jar, java=java, java_source=java_source,
        device_data_root=device_data_root, timing_data_dir=timing_data_dir)['route']


def validate_xilinx_route_db(path: Path, **kwargs) -> Dict[str, Any]:
    """Independently validate original route streams in the shared core."""
    from emuflow_tools.physical.rapidwright.route_validation import validate_native_route
    return validate_native_route(path, **kwargs)
