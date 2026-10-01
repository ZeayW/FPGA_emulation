"""One-shot execution of generated PPro black-box calibration bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Sequence

from .errors import ValidationError
from .io import read_json
from .ppro_blackbox_runner import cleanup_runtime_artifacts, execute_blackbox_queue
from .ppro_blackbox_runtime import PProRuntimeConfig, render_ppro_runtime_binding


_BUNDLE_FILES = {
    "capacity_probe.v",
    "communication_probe.v",
    "connected_smoke.v",
    "documented_constraints.json",
    "files.f",
    "parameters.json",
    "run-spec.json",
    "sources.f",
    "topology_probe.v",
}


@dataclass(frozen=True)
class PProCampaignRuntime:
    result_root: Path
    install_root: Path
    platform_reference: Path
    fpga_aliases: Mapping[str, str]
    logical_targets: Mapping[str, str]
    authorized_writable_root: Path = Path("/research/d4/gds/ziyiwang21")
    max_processes_per_case: int = 4
    utilization_limit_percent: int = 75
    timeout_seconds: float = 21600.0
    environment: Mapping[str, str] = field(default_factory=dict)


def discover_generated_bundles(root: Path, *, maximum_cases: int) -> list[Path]:
    if isinstance(maximum_cases, bool) or not isinstance(maximum_cases, int):
        raise ValidationError("PPro campaign maximum_cases must be an integer")
    if maximum_cases <= 0:
        raise ValidationError("PPro campaign maximum_cases must be positive")
    root = root.resolve()
    if not root.is_dir():
        raise ValidationError("PPro campaign bundle root does not exist")
    bundles = sorted(path.parent for path in root.rglob("run-spec.json"))
    if not bundles:
        raise ValidationError("PPro campaign bundle root contains no generated cases")
    if len(bundles) > maximum_cases:
        raise ValidationError("PPro campaign exceeds the explicit maximum case count")
    return bundles


def _bundle_inputs(root: Path) -> tuple[Path, Path, Path]:
    entries = list(root.iterdir()) if root.is_dir() else []
    if any(
        entry.is_symlink() or not entry.is_file() or entry.name not in _BUNDLE_FILES
        for entry in entries
    ):
        raise ValidationError("generated PPro bundle contains an unknown or unsafe entry")
    spec = root / "run-spec.json"
    constraints = root / "documented_constraints.json"
    filelists = [path for path in (root / "sources.f", root / "files.f") if path.is_file()]
    if not spec.is_file() or not constraints.is_file() or len(filelists) != 1:
        raise ValidationError(
            "generated PPro bundle requires run-spec.json, documented_constraints.json, "
            "and exactly one sources.f/files.f"
        )
    return spec, constraints, filelists[0]


def _cleanup_generated_bundle(root: Path) -> None:
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_file() or entry.name not in _BUNDLE_FILES:
            raise ValidationError("generated PPro bundle changed before cleanup")
    for entry in root.iterdir():
        entry.unlink()
    root.rmdir()


def prune_empty_bundle_tree(root: Path) -> None:
    """Remove empty matrix directories without deleting any remaining file."""

    root = root.resolve()
    if not root.exists():
        return
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def execute_generated_campaign(
    bundle_roots: Sequence[Path],
    *,
    runtime: PProCampaignRuntime,
    max_workers: int = 1,
) -> list[Dict[str, object]]:
    if not bundle_roots:
        raise ValidationError("PPro campaign requires at least one generated case")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
        raise ValidationError("PPro campaign max_workers must be a positive integer")
    result_root = runtime.result_root.resolve()
    writable_root = runtime.authorized_writable_root.resolve()
    if result_root == writable_root or not result_root.is_relative_to(writable_root):
        raise ValidationError("PPro campaign result root violates the authorized storage boundary")

    normalized_bundle_roots = sorted(path.resolve() for path in bundle_roots)
    if any(
        path == writable_root or not path.is_relative_to(writable_root)
        for path in normalized_bundle_roots
    ):
        raise ValidationError("PPro campaign input bundle violates the authorized storage boundary")

    cases = []
    identities: set[str] = set()
    try:
        for bundle_root in normalized_bundle_roots:
            spec_path, constraints_path, filelist_path = _bundle_inputs(bundle_root)
            spec = read_json(spec_path)
            identity = spec.get("identity", {}).get("id") if isinstance(spec, dict) else None
            if not isinstance(identity, str) or not identity:
                raise ValidationError("generated PPro bundle has no case identity")
            if identity in identities:
                raise ValidationError("PPro campaign contains duplicate case identities")
            identities.add(identity)
            binding = render_ppro_runtime_binding(
                spec,
                source_filelist=filelist_path,
                config=PProRuntimeConfig(
                    case_dir=result_root / identity,
                    install_root=runtime.install_root,
                    platform_reference=runtime.platform_reference,
                    documented_constraints=constraints_path,
                    fpga_aliases=runtime.fpga_aliases,
                    logical_targets=runtime.logical_targets,
                    authorized_writable_root=runtime.authorized_writable_root,
                    max_processes=runtime.max_processes_per_case,
                    utilization_limit_percent=runtime.utilization_limit_percent,
                    timeout_seconds=runtime.timeout_seconds,
                    environment=runtime.environment,
                ),
            )
            cases.append((spec, binding))
    except Exception:
        for _, binding in cases:
            cleanup_runtime_artifacts(binding)
            try:
                binding.case_dir.rmdir()
            except OSError:
                pass
        raise
    results = execute_blackbox_queue(cases, max_workers=max_workers)
    for bundle_root in normalized_bundle_roots:
        _cleanup_generated_bundle(bundle_root)
    return results
