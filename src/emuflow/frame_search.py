"""Controlled minimum-frame search across system routing and TDM."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .phase4 import run_phase4
from .phase5 import run_phase5
from .platform import Platform
from .runtime import (
    build_virtual_runtime,
    estimate_runtime_timing,
    validate_virtual_runtime,
)


FRAME_SEARCH_SCHEMA = "emuflow.frame-search/v4"


def validate_frame_search_report(report: Dict[str, Any]) -> Dict[str, Any]:
    if report.get("schema") != FRAME_SEARCH_SCHEMA:
        raise ValidationError("frame-search schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("frame-search report did not pass")
    maximum = report.get("maximum_frame_slots")
    selected = report.get("selected_frame_slots")
    attempts = report.get("attempts")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or isinstance(selected, bool)
        or not isinstance(selected, int)
        or not 2 <= selected <= maximum
        or not isinstance(attempts, list)
        or not attempts
    ):
        raise ValidationError("frame-search bounds or attempts are invalid")
    by_slots = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValidationError("frame-search attempt must be an object")
        slots = attempt.get("frame_slots")
        status = attempt.get("status")
        if (
            isinstance(slots, bool)
            or not isinstance(slots, int)
            or slots < 2
            or slots > maximum
            or slots in by_slots
            or status not in {"feasible", "infeasible"}
        ):
            raise ValidationError("frame-search attempt is invalid")
        if status == "feasible":
            expected_root = f"frame-{slots:08d}"
            if (
                attempt.get("route_dir")
                != f"{expected_root}/system-route"
                or attempt.get("tdm_dir") != f"{expected_root}/tdm"
                or isinstance(attempt.get("completion_slot"), bool)
                or not isinstance(attempt.get("completion_slot"), int)
                or attempt["completion_slot"] < 0
            ):
                raise ValidationError(
                    "frame-search feasible attempt metadata is invalid"
                )
        elif (
            not isinstance(attempt.get("error_type"), str)
            or not attempt["error_type"]
            or not isinstance(attempt.get("reason"), str)
            or not attempt["reason"]
        ):
            raise ValidationError(
                "frame-search infeasible attempt evidence is invalid"
            )
        by_slots[slots] = attempt
    if report.get("evaluated_candidates") != len(attempts):
        raise ValidationError("frame-search candidate count is inconsistent")
    if selected not in by_slots or by_slots[selected]["status"] != "feasible":
        raise ValidationError("selected frame-search candidate is not feasible")
    if maximum not in by_slots or by_slots[maximum]["status"] != "feasible":
        raise ValidationError("frame-search maximum was not proven feasible")
    if selected > 2 and (
        selected - 1 not in by_slots
        or by_slots[selected - 1]["status"] != "infeasible"
    ):
        raise ValidationError(
            "frame-search minimum boundary was not proven infeasible"
        )
    if any(
        slots < selected and attempt["status"] != "infeasible"
        for slots, attempt in by_slots.items()
    ):
        raise ValidationError("frame-search has a feasible smaller candidate")
    if report.get("selected_candidate") != f"frame-{selected:08d}":
        raise ValidationError("frame-search selected candidate is inconsistent")
    speedup = report.get("speedup_over_maximum")
    if (
        isinstance(speedup, bool)
        or not isinstance(speedup, (int, float))
        or abs(float(speedup) - maximum / selected) > 1.0e-12
    ):
        raise ValidationError("frame-search speedup is inconsistent")
    configuration = report.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError("frame-search configuration is invalid")
    expected_configuration_keys = {
        "ratio_max_iterations",
        "max_ratio",
        "ratio_quantum",
        "post_refinement_iterations",
        "slot_refinement_iterations",
        "tdm_provider",
        "ratio_convergence",
        "simulation_frames",
    }
    if set(configuration) != expected_configuration_keys:
        raise ValidationError("frame-search configuration is incomplete")
    for name in (
        "ratio_max_iterations",
        "ratio_quantum",
        "post_refinement_iterations",
        "simulation_frames",
    ):
        value = configuration[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(
                f"frame-search configuration {name} is invalid"
            )
    slot_iterations = configuration["slot_refinement_iterations"]
    if (
        isinstance(slot_iterations, bool)
        or not isinstance(slot_iterations, int)
        or slot_iterations < 0
    ):
        raise ValidationError(
            "frame-search configuration slot_refinement_iterations is invalid"
        )
    tdm_provider = configuration["tdm_provider"]
    if tdm_provider is not None and (
        not isinstance(tdm_provider, str) or not tdm_provider
    ):
        raise ValidationError(
            "frame-search configuration tdm_provider is invalid"
        )
    max_ratio = configuration["max_ratio"]
    if max_ratio is not None and (
        isinstance(max_ratio, bool)
        or not isinstance(max_ratio, int)
        or max_ratio <= 0
    ):
        raise ValidationError(
            "frame-search configuration max_ratio is invalid"
        )
    convergence = configuration["ratio_convergence"]
    if (
        isinstance(convergence, bool)
        or not isinstance(convergence, (int, float))
        or convergence <= 0.0
    ):
        raise ValidationError(
            "frame-search configuration ratio_convergence is invalid"
        )
    return {
        "status": "pass",
        "selected_frame_slots": selected,
        "evaluated_candidates": len(attempts),
        "speedup_over_maximum": float(speedup),
    }


def run_frame_length_search(
    assignment_path: Path,
    platform_path: Path,
    timing_paths_path: Optional[Path],
    search_root: Path,
    route_output_dir: Path,
    tdm_output_dir: Path,
    *,
    max_frame_slots: int,
    route_constraints: Optional[Path] = None,
    route_max_iterations: Optional[int] = None,
    router: Optional[str] = None,
    route_provider: Optional[str] = None,
    candidate_workers: int = 1,
    tdm_provider: Optional[str] = None,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
    simulation_frames: int = 16,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: Optional[int] = None,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    ratio_convergence: float = 1.0e-9,
) -> Dict[str, Any]:
    """Find the shortest feasible frame under the exact downstream checks.

    Feasibility is monotone in the capacity model: increasing the frame adds
    link bit-slots and ratio choices. Every accepted point is nevertheless
    rebuilt and checked by the real Phase 4 and Phase 5 providers.
    """
    if (
        isinstance(max_frame_slots, bool)
        or not isinstance(max_frame_slots, int)
        or max_frame_slots < 2
    ):
        raise ValidationError("frame-search maximum must be at least two")
    if route_output_dir.exists() or tdm_output_dir.exists():
        raise EmuFlowError(
            "frame-search canonical route/TDM outputs must not exist"
        )

    if search_root.exists() and any(search_root.iterdir()):
        raise EmuFlowError("frame-search output directory must be empty")
    search_root.mkdir(parents=True, exist_ok=True)
    platform = Platform.load(platform_path)
    attempts: Dict[int, Dict[str, Any]] = {}

    def evaluate(frame_slots: int) -> bool:
        if frame_slots in attempts:
            return attempts[frame_slots]["status"] == "feasible"
        root = search_root / f"frame-{frame_slots:08d}"
        route_root = root / "system-route"
        tdm_root = root / "tdm"
        try:
            phase4 = run_phase4(
                assignment_path,
                platform_path,
                route_root,
                constraints_path=route_constraints,
                frame_slots=frame_slots,
                max_iterations=route_max_iterations,
                timing_paths_path=timing_paths_path,
                router=router,
                provider=route_provider,
                candidate_workers=candidate_workers,
            )
            phase5 = run_phase5(
                route_root / "routes.json",
                platform_path,
                tdm_root,
                simulation_frames=simulation_frames,
                provider=tdm_provider,
                ratio_optimizer=ratio_optimizer,
                timing_dag_optimizer=timing_dag_optimizer,
                slot_optimizer=slot_optimizer,
                ratio_max_iterations=ratio_max_iterations,
                max_ratio=max_ratio,
                ratio_quantum=ratio_quantum,
                post_refinement_iterations=post_refinement_iterations,
                slot_refinement_iterations=slot_refinement_iterations,
                convergence=ratio_convergence,
            )
            schedule = read_json(tdm_root / "schedule.json")
            runtime = build_virtual_runtime(schedule, platform)
            runtime_validation = validate_virtual_runtime(
                runtime, schedule, platform
            )
            runtime_timing = estimate_runtime_timing(runtime, phase5)
            if runtime_timing["status"] == "fail":
                raise ValidationError(
                    "scheduled path delay exceeds the virtual clock period"
                )
        except (EmuFlowError, ValidationError) as error:
            attempts[frame_slots] = {
                "frame_slots": frame_slots,
                "status": "infeasible",
                "error_type": type(error).__name__,
                "reason": str(error),
            }
            return False
        attempts[frame_slots] = {
            "frame_slots": frame_slots,
            "status": "feasible",
            "completion_slot": phase5["validation"]["completion_slot"],
            "max_tdm_ratio": phase5["validation"].get("max_tdm_ratio"),
            "max_link_utilization": phase4["validation"][
                "max_link_utilization"
            ],
            "worst_normalized_slack": phase5.get(
                "timing_validation", {}
            ).get("worst_normalized_slack"),
            "nominal_virtual_frequency_mhz": runtime_validation[
                "nominal_virtual_frequency_mhz"
            ],
            "estimated_runtime_slack_ns": runtime_timing.get(
                "virtual_clock", {}
            ).get("estimated_worst_slack_ns"),
            "route_dir": str(route_root.relative_to(search_root)),
            "tdm_dir": str(tdm_root.relative_to(search_root)),
        }
        return True

    if not evaluate(max_frame_slots):
        raise EmuFlowError(
            "frame-search upper bound is infeasible; increase --frame-slots"
        )

    low = 2
    high = max_frame_slots
    while low < high:
        middle = (low + high) // 2
        if evaluate(middle):
            high = middle
        else:
            low = middle + 1
    chosen = low
    if chosen > 2 and evaluate(chosen - 1):
        # Defensive exact descent if a provider exposes a local non-monotone
        # heuristic result near the bisection boundary.
        while chosen > 2 and evaluate(chosen - 1):
            chosen -= 1

    selected_root = search_root / f"frame-{chosen:08d}"
    shutil.copytree(selected_root / "system-route", route_output_dir)
    shutil.copytree(selected_root / "tdm", tdm_output_dir)
    selected_routes = read_json(route_output_dir / "routes.json")
    resolved_ratio_quantum = (
        ratio_quantum
        if ratio_quantum is not None
        else int(selected_routes["constraints"].get("tdm_ratio_quantum", 8))
    )
    report = {
        "schema": FRAME_SEARCH_SCHEMA,
        "status": "pass",
        "provider": "checked-monotone-bisection-v1",
        "objective": "minimum-feasible-frame-slots",
        "maximum_frame_slots": max_frame_slots,
        "selected_frame_slots": chosen,
        "speedup_over_maximum": max_frame_slots / chosen,
        "evaluated_candidates": len(attempts),
        "configuration": {
            "ratio_max_iterations": ratio_max_iterations,
            "max_ratio": max_ratio,
            "ratio_quantum": resolved_ratio_quantum,
            "post_refinement_iterations": post_refinement_iterations,
            "slot_refinement_iterations": slot_refinement_iterations,
            "tdm_provider": tdm_provider,
            "ratio_convergence": ratio_convergence,
            "simulation_frames": simulation_frames,
        },
        "attempts": [attempts[key] for key in sorted(attempts)],
        "selected_candidate": str(selected_root.relative_to(search_root)),
    }
    validate_frame_search_report(report)
    write_json(search_root / "frame-search-report.json", report)
    return report
