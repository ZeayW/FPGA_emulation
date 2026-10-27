from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .phase3_storage import read_phase3_assignment_view
from .platform import Platform
from .routing import (
    demands_from_assignment,
)
from .timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    load_sta_paths,
    validate_native_system_routes,
)
from .system_route_storage import (
    read_system_routes,
)


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
    from .phase4_native import run_native_phase4
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
    assignment = read_phase3_assignment_view(
        assignment_path, include_semantic_contract=False
    )
    platform = Platform.load(platform_path)
    routes = read_system_routes(routes_path)
    if routes.get("provider") != GLOBAL_CANDIDATE_PROVIDER:
        raise ValueError("Phase 4 requires the global candidate provider")
    timing_paths = (
        load_sta_paths(timing_paths_path, demands_from_assignment(assignment, platform))
        if timing_paths_path is not None else None
    )
    if timing_paths is None and routes["routes"] and routes.get("timing_evaluation") is None:
        raise ValueError("global candidate route validation requires --timing-paths")
    return validate_native_system_routes(assignment, platform, routes, timing_paths)
