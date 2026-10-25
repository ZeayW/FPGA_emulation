from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .io import write_json
from .phase3_storage import read_phase3_assignment_view
from .platform import Platform
from .routing import (
    assignment_has_static_exact_contract,
    demands_from_assignment,
    load_route_constraints,
)
from .timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    annotate_system_route_timing,
    load_sta_paths,
    route_system_native,
    validate_native_system_routes,
)
from .system_route_storage import (
    read_system_routes,
    write_system_route_storage,
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
    retain_candidate_pool: bool = False,
    timing_driven: bool = True,
) -> Dict[str, Any]:
    if provider is None:
        provider = GLOBAL_CANDIDATE_PROVIDER
    if provider != GLOBAL_CANDIDATE_PROVIDER:
        raise ValueError(f"unsupported Phase 4 routing provider {provider!r}")
    if not isinstance(timing_driven, bool):
        raise ValueError("timing_driven must be a boolean")
    assignment = read_phase3_assignment_view(
        assignment_path, include_semantic_contract=False
    )
    platform = Platform.load(platform_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_pool_path = (
        output_dir / "route_candidate_pool.json"
        if retain_candidate_pool
        else None
    )
    constraints = load_route_constraints(
        constraints_path,
        platform,
        frame_slots=frame_slots,
        max_iterations=max_iterations,
    )
    exact_mode = assignment_has_static_exact_contract(assignment)
    timing_paths = None
    if timing_paths_path is not None:
        timing_paths = load_sta_paths(
            timing_paths_path, demands_from_assignment(assignment, platform)
        )
    elif timing_driven and demands_from_assignment(assignment, platform):
        raise ValueError("global candidate routing requires --timing-paths")
    routes = route_system_native(
        assignment, platform, constraints,
        timing_paths if timing_driven else None,
        executable=router, provider=provider,
        candidate_pool_path=candidate_pool_path,
        candidate_workers=candidate_workers, materialize_result=False,
    )
    if not timing_driven and timing_paths is not None:
        routes = annotate_system_route_timing(assignment, platform, routes, timing_paths)
    validation = validate_native_system_routes(assignment, platform, routes, timing_paths)
    report: Dict[str, Any] = {
        "schema": PHASE4_REPORT_SCHEMA,
        "phase": 4,
        "status": "pass",
        "design": routes["design"],
        "platform": platform.name,
        "provider": routes["provider"],
        "validation": validation,
        "artifacts": {
            "constraints": "route_constraints.normalized.json",
            "routes": "routes.json",
            "report": "phase4_report.json",
        },
    }
    if exact_mode:
        report["cut_mode"] = "static-exact-combinational"
        report["qualification"] = "route-contract-propagation-pass"
    # Seal the execution contract for every native provider.  The experiment
    # checkpoint validator must be able to distinguish a deterministic
    # single-worker exact-cut route from a differently configured run even
    # when Phase 4 only annotates timing after routing and therefore has no
    # persisted candidate pool.
    report["candidate_generation"] = {
        "requested_workers": candidate_workers,
        "ordering": "demand-index-then-generator-index",
        "route_artifact_deterministic": True,
        "validation_dataflow": (
            "materialized-diagnostic-candidate-pool-v1"
            if candidate_pool_path is not None
            else "streamed-candidate-domain-footprint-v1"
        ),
    }
    if candidate_pool_path is not None and candidate_pool_path.is_file():
        report["artifacts"]["candidate_pool"] = "route_candidate_pool.json"
    write_json(output_dir / "route_constraints.normalized.json", constraints)
    route_records = routes.get("routes")
    direct_native_route_relations = bool(
        getattr(route_records, "has_native_route_relations", False)
    )
    try:
        route_storage = write_system_route_storage(
            output_dir / "routes.json", routes
        )
    finally:
        release_native_routes = getattr(
            route_records, "release_native_route_relations", None
        )
        if callable(release_native_routes):
            release_native_routes()
    report["artifacts"]["route_store"] = route_storage["artifact"]
    report["route_storage"] = {
        "schema": route_storage["schema"],
        "user_version": route_storage["user_version"],
        "dataflow": (
            "native-sql-route-arc-delay-selection-publication-v1"
            if direct_native_route_relations
            else "typed-sqlite-route-scalar-timing-selection-batch-relations-v3"
        ),
        "bytes": route_storage["store_bytes"],
        "logical_sha256": route_storage["logical_sha256"],
        "records": {
            section: seal["records"]
            for section, seal in route_storage["sections"].items()
        },
    }
    write_json(output_dir / "phase4_report.json", report)
    return report


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
