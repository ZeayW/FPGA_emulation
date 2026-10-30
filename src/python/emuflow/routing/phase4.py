from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional



GLOBAL_CANDIDATE_PROVIDER = "timing-aware-global-candidate-v1"

PHASE4_REPORT_SCHEMA = "emuflow.phase4-report/v1"


def run_phase4(
    assignment_path: Path,
    platform_path: Path,
    output_dir: Path,
    constraints_path: Optional[Path] = None,
    frame_slots: Optional[int] = None,
    max_iterations: Optional[int] = None,
    provider: Optional[str] = None,
    timing_paths_path: Optional[Path] = None,
    router: Optional[str] = None,
    candidate_workers: int = 1,
    timing_driven: bool = True,
) -> Dict[str, Any]:
    """Invoke the single native stage using artifact references and bounded policy."""
    if provider not in (None, GLOBAL_CANDIDATE_PROVIDER):
        raise ValueError(f"unsupported Phase 4 routing provider {provider!r}")
    from emuflow.routing.phase4_native import run_native_phase4
    return run_native_phase4(assignment_path, platform_path, output_dir,
        timing_paths=timing_paths_path, constraints_path=constraints_path,
        frame_slots=frame_slots, max_iterations=max_iterations,
        candidate_workers=candidate_workers, timing_driven=timing_driven,
        executable=router)


def validate_phase4(
    assignment_path: Path,
    platform_path: Path,
    routes_path: Path,
    timing_paths_path: Optional[Path] = None,
) -> Dict[str, Any]:
    from emuflow.routing.phase4_native import validate_native_phase4
    return validate_native_phase4(assignment_path, platform_path, routes_path, timing_paths_path)
