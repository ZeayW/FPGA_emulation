"""Bounded native pipeline hosts and shared immutable-generation leases."""
from __future__ import annotations
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Mapping, Optional
from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow_tools.transport.chimew.pipeline_native import audit_native_chimew_pipeline

CHIMEW_PIPELINE_REPORT_SCHEMA="emuflow.chimew-phase6-pipeline-report/v1"
CHIMEW_PIPELINE_PROVIDER="source-qualified-chimew-phase6-pipeline-v1"
CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER="byte-bound-chimew-phase6-pipeline-v2"
CHIMEW_PIPELINE_GENERATION_SCHEMA="emuflow.chimew-pipeline-generation/v2"

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _pipeline_lock(output_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize one output's writers and lease its generations to readers."""

    output_dir = Path(output_dir).resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir.parent / f".{output_dir.name}.pipeline.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)



def _artifact_path(output_dir: Path, label: str, record: Any) -> Path:
    if not isinstance(record, dict):
        raise ValidationError(f"Chimew pipeline artifact {label!r} is invalid")
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError(f"Chimew pipeline artifact {label!r} path is invalid")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"Chimew pipeline artifact {label!r} path is unsafe")
    root = output_dir.resolve()
    path = (root / Path(*relative.parts)).resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise ValidationError(f"Chimew pipeline artifact {label!r} is missing")
    supplied_sha = record.get("sha256")
    if not isinstance(supplied_sha, str) or _sha256(path) != supplied_sha:
        raise ValidationError(f"Chimew pipeline artifact {label!r} hash differs")
    return path



def run_chimew_phase6_pipeline(
    schedule_path: Path,
    platform_path: Path,
    crossings_path: Path,
    positions_path: Path,
    rudy_input_path: Path,
    bank_channel_input_path: Path,
    electrical_map_path: Path,
    output_dir: Path,
    *,
    source_paths: Optional[Mapping[str, Path]] = None,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
    rudy: Optional[str] = None,
    assigner: Optional[str] = None,
    region_count: int = 31,
) -> Dict[str, Any]:
    """Delegate complete generation publication and its exclusive lock to C++."""
    from emuflow_tools.transport.chimew.pipeline_native import run_native_chimew_pipeline
    return run_native_chimew_pipeline(schedule_path, platform_path, crossings_path, positions_path,
        rudy_input_path, bank_channel_input_path, electrical_map_path, output_dir,
        source_paths=source_paths, region_count=region_count, executables=(grouper,refiner,rudy,assigner))


@contextmanager
def read_chimew_phase6_pipeline(output_dir: Path, *, expected_report_sha256: Optional[str] = None):
    """Lease a native-audited commit and its complete generation inventory."""
    with _pipeline_lock(output_dir, exclusive=False):
        report_path=Path(output_dir)/"pipeline_report.json"
        if expected_report_sha256 is not None and _sha256(report_path)!=expected_report_sha256:
            raise ValidationError("Chimew pipeline report hash differs")
        validation=audit_native_chimew_pipeline(output_dir)
        yield read_json(report_path), validation


def validate_chimew_phase6_pipeline(output_dir: Path) -> Dict[str, Any]:
    with read_chimew_phase6_pipeline(output_dir) as (_report,validation):
        return dict(validation)
