"""Validated, storage-bounded archives for completed multi-FPGA runs."""

from __future__ import annotations

import hashlib
import os
import platform as host_platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from . import __version__
from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .multi_fpga_flow import validate_multi_fpga_flow_report


VALIDATION_ARCHIVE_SCHEMA = "emuflow.validation-archive/v1"
CLEANUP_SEAL_SCHEMA = "emuflow.validation-cleanup-seal/v1"
CLEANUP_RECEIPT_SCHEMA = "emuflow.validation-cleanup-receipt/v1"
DEFAULT_MAX_COPY_BYTES = 64 * 1024 * 1024

_REPORT_BASENAME_TOKENS = (
    "report",
    "summary",
    "manifest",
    "contract",
    "validation",
    "constraints",
    "timing",
    "weights",
)
_IMPORTANT_JSON_NAMES = {
    "assignment.json",
    "routes.json",
    "schedule.json",
    "ratio_plan.json",
    "platform.normalized.json",
    "synthesized.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _checked_relative_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValidationError(
            f"archive artifact path must stay below the flow root: {raw_path!r}"
        )
    return path


def _source_file(flow_root: Path, relative: Path) -> Path:
    path = flow_root / relative
    if path.is_symlink():
        raise ValidationError(
            f"archive refuses symbolic-link artifact: {relative.as_posix()}"
        )
    resolved = path.resolve()
    if not _is_relative_to(resolved, flow_root):
        raise ValidationError(
            f"archive artifact escapes the flow root: {relative.as_posix()}"
        )
    if not resolved.is_file():
        raise ValidationError(
            f"archive artifact is missing: {relative.as_posix()}"
        )
    return resolved


def _git_revision() -> Dict[str, Any]:
    source_root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status.strip())}


def _is_report_like(path: Path) -> bool:
    if path.suffix != ".json":
        return False
    name = path.name.lower()
    return (
        name in _IMPORTANT_JSON_NAMES
        or name.endswith(".normalized.json")
        or any(token in name for token in _REPORT_BASENAME_TOKENS)
    )


def _declared_artifacts(report: Mapping[str, Any]) -> Dict[Path, set[str]]:
    result: Dict[Path, set[str]] = {}
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValidationError("multi-FPGA flow report has no artifact map")
    for role, artifact in artifacts.items():
        if not isinstance(role, str) or not isinstance(artifact, dict):
            raise ValidationError("multi-FPGA artifact map is malformed")
        raw_path = artifact.get("path")
        expected = artifact.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            raise ValidationError(
                f"multi-FPGA artifact {role!r} lacks path/SHA-256"
            )
        relative = _checked_relative_path(raw_path)
        result.setdefault(relative, set()).add(role)
    return result


def _reported_artifacts(
    report: Mapping[str, Any], flow_root: Path
) -> tuple[
    Dict[Path, set[str]],
    Dict[Path, set[str]],
    list[Dict[str, Any]],
    list[Dict[str, Any]],
]:
    internal = _declared_artifacts(report)
    expected: Dict[Path, set[str]] = {}
    external: Dict[tuple[str, str], Dict[str, Any]] = {}
    pruned: Dict[tuple[str, str], Dict[str, Any]] = {}

    for relative, roles in internal.items():
        for role in roles:
            expected.setdefault(relative, set()).add(
                report["artifacts"][role]["sha256"]
            )

    def visit(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            raw_path = value.get("path")
            digest = value.get("sha256")
            if (
                isinstance(raw_path, str)
                and isinstance(digest, str)
                and len(digest) == 64
                and all(
                    character in "0123456789abcdef" for character in digest
                )
            ):
                path = Path(raw_path).expanduser()
                resolved = (
                    path.resolve()
                    if path.is_absolute()
                    else (flow_root / path).resolve()
                )
                if _is_relative_to(resolved, flow_root):
                    relative = resolved.relative_to(flow_root)
                    if value.get("retained") is False:
                        record = {
                            "source_path": relative.as_posix(),
                            "status": "intentionally-pruned",
                            "expected_sha256": digest,
                            "role": f"reported:{pointer}",
                        }
                        size = value.get("bytes")
                        if (
                            not isinstance(size, bool)
                            and isinstance(size, int)
                            and size >= 0
                        ):
                            record["bytes"] = size
                        pruned[(relative.as_posix(), digest)] = record
                        for key, item in value.items():
                            visit(item, f"{pointer}/{key}")
                        return
                    internal.setdefault(relative, set()).add(
                        f"reported:{pointer}"
                    )
                    expected.setdefault(relative, set()).add(digest)
                else:
                    key = (str(resolved), digest)
                    record: Dict[str, Any] = {
                        "path": str(resolved),
                        "status": "unavailable",
                        "expected_sha256": digest,
                        "role": f"reported:{pointer}",
                    }
                    if resolved.is_file() and not resolved.is_symlink():
                        actual = _sha256(resolved)
                        if actual != digest:
                            raise ValidationError(
                                "external artifact SHA-256 disagrees with "
                                f"flow report: {resolved}"
                            )
                        record.update(
                            {
                                "status": "verified-hash-only",
                                "bytes": resolved.stat().st_size,
                                "sha256": actual,
                            }
                        )
                    external[key] = record
            for key, item in value.items():
                visit(item, f"{pointer}/{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{pointer}/{index}")

    visit(report, "$")
    return (
        internal,
        expected,
        [external[key] for key in sorted(external)],
        [pruned[key] for key in sorted(pruned)],
    )


def _candidate_files(
    flow_root: Path,
    reported: Mapping[Path, set[str]],
) -> Dict[Path, set[str]]:
    result = {path: set(roles) for path, roles in reported.items()}
    result.setdefault(Path("multi-fpga-flow-report.json"), set()).add(
        "flow_report"
    )
    for path in flow_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(flow_root)
        result.setdefault(relative, set()).add(
            "discovered_record" if _is_report_like(path) else "unclassified-run-artifact"
        )
    return result


def _external_inputs(report: Mapping[str, Any]) -> list[Dict[str, Any]]:
    frontend = report.get("stages", {}).get("frontend", {})
    synthesis = frontend.get("synthesis", {}) if isinstance(frontend, dict) else {}
    sources = synthesis.get("sources", []) if isinstance(synthesis, dict) else []
    records = []
    if not isinstance(sources, list):
        return records
    for raw_path in sorted(set(item for item in sources if isinstance(item, str))):
        path = Path(raw_path).expanduser()
        if not path.is_file() or path.is_symlink():
            records.append({"path": raw_path, "status": "unavailable"})
            continue
        records.append(
            {
                "path": str(path.resolve()),
                "status": "hash-only",
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _merge_external_inputs(
    sources: Iterable[Dict[str, Any]], reported: Iterable[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    records: Dict[tuple[str, str], Dict[str, Any]] = {}
    for record in (*sources, *reported):
        path = record.get("path")
        digest = record.get("sha256", record.get("expected_sha256", ""))
        if isinstance(path, str) and isinstance(digest, str):
            records[(path, digest)] = record
    return [records[key] for key in sorted(records)]


def _validate_json(path: Path) -> None:
    if path.suffix == ".json":
        read_json(path)


def create_validation_archive(
    flow_root: Path,
    archive_dir: Path,
    *,
    run_id: str,
    source_commit: Optional[str] = None,
    max_copy_bytes: int = DEFAULT_MAX_COPY_BYTES,
    tool_versions: Optional[Mapping[str, str]] = None,
    run_configuration: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create an atomic archive from a checked full-flow output directory."""

    if not run_id.strip():
        raise EmuFlowError("archive run ID must not be empty")
    if isinstance(max_copy_bytes, bool) or max_copy_bytes < 0:
        raise EmuFlowError("archive max-copy-bytes must be non-negative")
    flow_root = flow_root.resolve()
    archive_dir = archive_dir.resolve()
    if not flow_root.is_dir():
        raise EmuFlowError(f"flow root does not exist: {flow_root}")
    if _is_relative_to(archive_dir, flow_root) or _is_relative_to(
        flow_root, archive_dir
    ):
        raise EmuFlowError(
            "archive and flow roots must be separate, non-nested directories"
        )
    if archive_dir.exists():
        raise EmuFlowError(f"archive output already exists: {archive_dir}")

    report_path = flow_root / "multi-fpga-flow-report.json"
    report = read_json(report_path)
    summary = validate_multi_fpga_flow_report(report)
    reported, expected, reported_external, pruned = _reported_artifacts(
        report, flow_root
    )
    candidates = _candidate_files(flow_root, reported)

    temporary = archive_dir.with_name(
        f".{archive_dir.name}.creating-{os.getpid()}"
    )
    if temporary.exists():
        raise EmuFlowError(f"archive staging path already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        files = []
        verified_declared = 0
        for relative in sorted(candidates, key=lambda item: item.as_posix()):
            source = _source_file(flow_root, relative)
            digest = _sha256(source)
            roles = sorted(candidates[relative])
            artifact_roles = sorted(reported.get(relative, set()))
            for expected_digest in sorted(expected.get(relative, set())):
                if digest != expected_digest:
                    raise ValidationError(
                        "artifact SHA-256 disagrees with flow report: "
                        f"{relative.as_posix()}"
                    )
            verified_declared += len(artifact_roles)
            size = source.stat().st_size
            retained = (
                relative == Path("multi-fpga-flow-report.json")
                or size <= max_copy_bytes
            )
            archive_path = None
            if retained:
                destination = temporary / "files" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if _sha256(destination) != digest:
                    raise ValidationError(
                        f"copied archive artifact changed: {relative.as_posix()}"
                    )
                archive_path = (Path("files") / relative).as_posix()
            files.append(
                {
                    "source_path": relative.as_posix(),
                    "retention": "copied" if retained else "hash-only",
                    "archive_path": archive_path,
                    "bytes": size,
                    "sha256": digest,
                    "roles": roles,
                }
            )

        # Validate only after every retained file has been copied.  Compact
        # Phase 3 assignments intentionally reference their sibling
        # clusters.json, so validating files one-at-a-time made archive
        # correctness depend on lexical copy order.
        for item in files:
            archive_path = item.get("archive_path")
            if isinstance(archive_path, str):
                _validate_json(temporary / archive_path)

        revision = _git_revision()
        if source_commit is not None:
            revision["commit"] = source_commit
        manifest = {
            "schema": VALIDATION_ARCHIVE_SCHEMA,
            "status": "pass",
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "flow_root": str(flow_root),
                "flow_report": "multi-fpga-flow-report.json",
                "revision": revision,
                "external_inputs": _merge_external_inputs(
                    _external_inputs(report), reported_external
                ),
                "pruned_artifacts": pruned,
            },
            "flow": {
                "schema": report.get("schema"),
                "provider": report.get("provider"),
                "summary": summary,
            },
            "environment": {
                "emuflow_version": __version__,
                "python": sys.version.split()[0],
                "platform": host_platform.platform(),
                "tools": dict(sorted((tool_versions or {}).items())),
            },
            "run_configuration": dict(run_configuration or {}),
            "retention_policy": {
                "max_copy_bytes": max_copy_bytes,
                "large_artifacts": "sha256-and-size-only",
                "symbolic_links": "rejected",
                "source_cleanup_requires_zero_hash_only_files": True,
            },
            "files": files,
            "validation": {
                "flow_report": "pass",
                "declared_artifacts_verified": verified_declared,
                "copied_files_verified": sum(
                    item["retention"] == "copied" for item in files
                ),
            },
        }
        write_json(temporary / "archive-manifest.json", manifest)
        manifest_sha256 = _sha256(temporary / "archive-manifest.json")
        seal = {
            "schema": CLEANUP_SEAL_SCHEMA,
            "status": "sealed",
            "source_flow_root": str(flow_root),
            "manifest_sha256": manifest_sha256,
        }
        write_json(temporary / "cleanup-seal.json", seal)
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(archive_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return validate_validation_archive(archive_dir)


def _validate_file_record(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValidationError("archive file record must be an object")
    if record.get("retention") not in {"copied", "hash-only"}:
        raise ValidationError("archive file retention mode is invalid")
    if not isinstance(record.get("source_path"), str):
        raise ValidationError("archive file source path is invalid")
    _checked_relative_path(record["source_path"])
    size = record.get("bytes")
    digest = record.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValidationError("archive file size is invalid")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValidationError("archive file SHA-256 is invalid")


def validate_validation_archive(archive_dir: Path) -> Dict[str, Any]:
    """Validate archive content without requiring its original run directory."""

    archive_dir = archive_dir.resolve()
    manifest_path = archive_dir / "archive-manifest.json"
    seal_path = archive_dir / "cleanup-seal.json"
    manifest = read_json(manifest_path)
    seal = read_json(seal_path)
    if manifest.get("schema") != VALIDATION_ARCHIVE_SCHEMA:
        raise ValidationError("validation archive schema is invalid")
    if manifest.get("status") != "pass":
        raise ValidationError("validation archive did not pass")
    if seal.get("schema") != CLEANUP_SEAL_SCHEMA:
        raise ValidationError("validation archive cleanup seal is invalid")
    if seal.get("manifest_sha256") != _sha256(manifest_path):
        raise ValidationError("validation archive manifest seal is broken")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or seal.get("source_flow_root") != source.get("flow_root")
    ):
        raise ValidationError("validation archive source-root seal is broken")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("validation archive has no file records")
    copied = 0
    hash_only = 0
    flow_report_record = None
    seen = set()
    for record in files:
        _validate_file_record(record)
        source_path = record["source_path"]
        if source_path in seen:
            raise ValidationError(f"duplicate archive file record: {source_path}")
        seen.add(source_path)
        if source_path == source.get("flow_report"):
            flow_report_record = record
        if record["retention"] == "hash-only":
            if record.get("archive_path") is not None:
                raise ValidationError("hash-only archive file has a copied path")
            hash_only += 1
            continue
        raw_archive_path = record.get("archive_path")
        if not isinstance(raw_archive_path, str):
            raise ValidationError("copied archive file has no archive path")
        relative = _checked_relative_path(raw_archive_path)
        path = _source_file(archive_dir, relative)
        if path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]:
            raise ValidationError(
                f"copied archive file failed integrity check: {raw_archive_path}"
            )
        _validate_json(path)
        copied += 1
    if flow_report_record is None or flow_report_record["retention"] != "copied":
        raise ValidationError("archive must retain the complete flow report")
    archived_report = read_json(
        archive_dir / flow_report_record["archive_path"]
    )
    summary = validate_multi_fpga_flow_report(archived_report)
    if summary != manifest.get("flow", {}).get("summary"):
        raise ValidationError("archived flow summary disagrees with manifest")
    return {
        "status": "pass",
        "run_id": manifest.get("run_id"),
        "copied_files": copied,
        "hash_only_files": hash_only,
        "flow": summary,
        "manifest_sha256": seal["manifest_sha256"],
    }


def cleanup_validation_source(
    archive_dir: Path, flow_root: Path
) -> Dict[str, Any]:
    """Remove a run directory only after archive and source revalidation."""

    archive_dir = archive_dir.resolve()
    validation = validate_validation_archive(archive_dir)
    manifest = read_json(archive_dir / "archive-manifest.json")
    hash_only = [
        record for record in manifest.get("files", [])
        if record.get("retention") == "hash-only"
    ]
    if hash_only:
        raise ValidationError(
            "cleanup refuses a non-replayable archive with hash-only files; "
            "retain the source run or create a role-complete experiment evidence bundle"
        )
    expected_root = Path(manifest["source"]["flow_root"])
    flow_root = flow_root.resolve()
    if flow_root != expected_root:
        raise ValidationError(
            "cleanup flow root does not match the source root sealed in archive"
        )
    if not flow_root.is_dir():
        raise ValidationError(f"cleanup source root does not exist: {flow_root}")
    if _is_relative_to(archive_dir, flow_root) or _is_relative_to(
        flow_root, archive_dir
    ):
        raise ValidationError("cleanup refuses nested flow/archive roots")
    if flow_root in {Path("/").resolve(), Path.home().resolve()} or len(
        flow_root.parts
    ) < 3:
        raise ValidationError(f"cleanup refuses unsafe source root: {flow_root}")

    report = read_json(flow_root / "multi-fpga-flow-report.json")
    validate_multi_fpga_flow_report(report)
    for record in manifest["files"]:
        source = _source_file(
            flow_root, _checked_relative_path(record["source_path"])
        )
        if (
            source.stat().st_size != record["bytes"]
            or _sha256(source) != record["sha256"]
        ):
            raise ValidationError(
                "cleanup source changed after archiving: "
                f"{record['source_path']}"
            )

    shutil.rmtree(flow_root)
    receipt = {
        "schema": CLEANUP_RECEIPT_SCHEMA,
        "status": "removed",
        "source_flow_root": str(flow_root),
        "archive_manifest_sha256": validation["manifest_sha256"],
    }
    write_json(archive_dir / "cleanup-receipt.json", receipt)
    return receipt
