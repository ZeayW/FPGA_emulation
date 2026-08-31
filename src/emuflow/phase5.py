from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .errors import TDMScheduleInfeasibleError
from .io import read_json, write_json
from .managed_json_storage import pack_managed_json
from .platform import Platform
from .tdm import (
    TDM_BASELINE_PROVIDER,
    TDM_STATIC_EXACT_PROVIDER,
    build_tdm_schedule,
    build_transport_manifest,
    reconstruct_tdm_schedule_timing,
    schedule_to_systemverilog_testbench,
    schedule_to_tsv,
    simulate_tdm_schedule,
    validate_tdm_schedule,
)
from .tdm_ratio import (
    DEFAULT_EXACT_DOMAIN_LIMIT,
    TDM_RATIO_PROVIDER,
    TDM_TIMING_DAG_RATIO_PROVIDER,
    _prepare_model,
    build_tdm_ratio_plan,
    validate_tdm_ratio_plan,
)
from .tdm_slot import refine_tdm_schedule_native
from .tdm_timing_dag import build_timing_dag_ratio_plan
from .tdm_feedback import build_tdm_feedback, validate_tdm_feedback


PHASE5_REPORT_SCHEMA = "emuflow.phase5-report/v1"


def run_phase5(
    routes_path: Path,
    platform_path: Path,
    output_dir: Path,
    simulation_frames: int = 16,
    provider: Optional[str] = None,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: Optional[int] = None,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    convergence: float = 1.0e-9,
    managed_storage: bool = False,
) -> Dict[str, Any]:
    if (
        isinstance(slot_refinement_iterations, bool)
        or not isinstance(slot_refinement_iterations, int)
        or slot_refinement_iterations < 0
    ):
        raise ValueError(
            "slot_refinement_iterations must be a non-negative integer"
        )
    routes = read_json(routes_path)
    platform = Platform.load(platform_path)
    exact_mode = routes.get("semantic_contract") is not None
    if provider is None:
        if exact_mode:
            provider = TDM_STATIC_EXACT_PROVIDER
        else:
            provider = (
                TDM_TIMING_DAG_RATIO_PROVIDER
                if isinstance(routes.get("timing"), dict)
                else TDM_BASELINE_PROVIDER
            )
    if exact_mode and provider != TDM_STATIC_EXACT_PROVIDER:
        raise ValueError(
            "static exact routes require the dependency-aware Phase 5 provider"
        )
    ratio_plan = None
    ratio_validation = None
    schedule = None
    validation = None
    timing_validation = None
    candidate_selection = None
    prepared_ratio_model = None
    if provider == TDM_STATIC_EXACT_PROVIDER:
        if not exact_mode:
            raise ValueError(
                "static exact Phase 5 provider requires a routed semantic "
                "contract"
            )
        if (
            ratio_optimizer is not None
            or timing_dag_optimizer is not None
            or slot_optimizer is not None
            or slot_refinement_iterations != 0
        ):
            raise ValueError(
                "static exact Phase 5 does not accept ratio/slot optimizers"
            )
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        if isinstance(routes.get("timing"), dict):
            timing_validation = reconstruct_tdm_schedule_timing(
                routes, platform, schedule
            )
    elif provider == TDM_BASELINE_PROVIDER:
        if exact_mode:
            raise ValueError(
                "static exact routes require the dependency-aware provider"
            )
        if (
            ratio_optimizer is not None
            or timing_dag_optimizer is not None
            or slot_optimizer is not None
        ):
            raise ValueError(
                "native TDM optimizers require the academic Phase 5 provider"
            )
    elif provider in {TDM_RATIO_PROVIDER, TDM_TIMING_DAG_RATIO_PROVIDER}:
        prepared_ratio_model = _prepare_model(routes, platform)
        if (
            provider == TDM_RATIO_PROVIDER
            and timing_dag_optimizer is not None
        ):
            raise ValueError(
                "timing_dag_optimizer requires the timing-DAG Phase 5 "
                "provider"
            )
        candidates = []
        rejected_candidates = []
        for strategy, exact_domain_limit in (
            ("exact-displacement-dp", DEFAULT_EXACT_DOMAIN_LIMIT),
            ("scalable-minimum-wire", 0),
        ):
            if provider == TDM_RATIO_PROVIDER:
                candidate_plan = build_tdm_ratio_plan(
                    routes,
                    platform,
                    executable=ratio_optimizer,
                    max_iterations=ratio_max_iterations,
                    max_ratio=max_ratio,
                    ratio_quantum=ratio_quantum,
                    post_refinement_iterations=post_refinement_iterations,
                    exact_domain_limit=exact_domain_limit,
                    convergence=convergence,
                    prepared_model=prepared_ratio_model,
                )
            else:
                candidate_plan = build_timing_dag_ratio_plan(
                    routes,
                    platform,
                    dag_executable=timing_dag_optimizer,
                    legalization_executable=ratio_optimizer,
                    max_iterations=ratio_max_iterations,
                    max_ratio=max_ratio,
                    ratio_quantum=ratio_quantum,
                    post_refinement_iterations=(
                        post_refinement_iterations
                    ),
                    exact_domain_limit=exact_domain_limit,
                    convergence=convergence,
                    prepared_model=prepared_ratio_model,
                )
            candidate_ratio_validation = validate_tdm_ratio_plan(
                routes,
                platform,
                candidate_plan,
                prepared_model=prepared_ratio_model,
            )
            try:
                candidate_schedule = build_tdm_schedule(
                    routes,
                    platform,
                    candidate_plan,
                    prepared_ratio_model=prepared_ratio_model,
                )
            except TDMScheduleInfeasibleError as error:
                rejected_candidates.append(
                    {
                        "strategy": strategy,
                        "status": "infeasible",
                        "reason": str(error),
                    }
                )
                continue
            if slot_refinement_iterations > 0:
                candidate_schedule = refine_tdm_schedule_native(
                    routes,
                    platform,
                    candidate_plan,
                    candidate_schedule,
                    executable=slot_optimizer,
                    max_iterations=slot_refinement_iterations,
                    prepared_ratio_model=prepared_ratio_model,
                )
            candidate_validation = validate_tdm_schedule(
                routes,
                platform,
                candidate_schedule,
                candidate_plan,
                prepared_ratio_model=prepared_ratio_model,
            )
            candidate_timing = reconstruct_tdm_schedule_timing(
                routes,
                platform,
                candidate_schedule,
                model=prepared_ratio_model,
            )
            score = (
                candidate_timing["worst_normalized_slack"],
                candidate_timing["p01_normalized_slack"],
                candidate_timing["median_normalized_slack"],
                -candidate_schedule["metrics"]["completion_slot"],
                candidate_plan["metrics"][
                    "discrete_worst_normalized_slack"
                ],
                1 if strategy == "exact-displacement-dp" else 0,
            )
            candidates.append(
                {
                    "strategy": strategy,
                    "score": score,
                    "ratio_plan": candidate_plan,
                    "ratio_validation": candidate_ratio_validation,
                    "schedule": candidate_schedule,
                    "validation": candidate_validation,
                    "timing_validation": candidate_timing,
                }
            )
        if not candidates:
            reasons = "; ".join(
                f"{candidate['strategy']}: {candidate['reason']}"
                for candidate in rejected_candidates
            )
            raise TDMScheduleInfeasibleError(
                "all academic Phase 5 candidates are infeasible: "
                f"{reasons}"
            )
        selected = max(candidates, key=lambda candidate: candidate["score"])
        ratio_plan = selected["ratio_plan"]
        ratio_validation = selected["ratio_validation"]
        schedule = selected["schedule"]
        validation = selected["validation"]
        timing_validation = selected["timing_validation"]
        candidate_selection = {
            "objective": (
                "lexicographic realized worst, p01, and median normalized "
                "slack; completion slot; analytical discrete slack"
            ),
            "selected": selected["strategy"],
            **(
                {"rejected_candidates": rejected_candidates}
                if rejected_candidates
                else {}
            ),
            "candidates": [
                {
                    "strategy": candidate["strategy"],
                    "realized_worst_normalized_slack": candidate[
                        "timing_validation"
                    ]["worst_normalized_slack"],
                    "realized_p01_normalized_slack": candidate[
                        "timing_validation"
                    ]["p01_normalized_slack"],
                    "realized_median_normalized_slack": candidate[
                        "timing_validation"
                    ]["median_normalized_slack"],
                    "completion_slot": candidate["schedule"]["metrics"][
                        "completion_slot"
                    ],
                    "analytical_worst_normalized_slack": candidate[
                        "ratio_plan"
                    ]["metrics"]["discrete_worst_normalized_slack"],
                    "dp_legalized_domains": candidate["ratio_plan"][
                        "metrics"
                    ]["dp_legalized_domains"],
                    "greedy_legalized_domains": candidate["ratio_plan"][
                        "metrics"
                    ]["greedy_legalized_domains"],
                    **(
                        {
                            "slot_accepted_moves": candidate["schedule"][
                                "slot_optimization"
                            ]["metrics"]["accepted_moves"],
                            "slot_evaluated_moves": candidate["schedule"][
                                "slot_optimization"
                            ]["metrics"]["evaluated_moves"],
                        }
                        if "slot_optimization" in candidate["schedule"]
                        else {}
                    ),
                }
                for candidate in candidates
            ],
        }
    else:
        raise ValueError(f"unsupported Phase 5 provider {provider!r}")
    if schedule is None:
        schedule = build_tdm_schedule(routes, platform, ratio_plan)
        validation = validate_tdm_schedule(
            routes, platform, schedule, ratio_plan
        )
        timing_validation = (
            reconstruct_tdm_schedule_timing(
                routes,
                platform,
                schedule,
                model=prepared_ratio_model,
            )
            if isinstance(routes.get("timing"), dict)
            else None
        )
    simulation = simulate_tdm_schedule(
        routes,
        schedule,
        frames=simulation_frames,
    )
    manifest = build_transport_manifest(routes, schedule, platform)
    feedback = build_tdm_feedback(
        routes,
        platform,
        schedule,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    feedback_validation = validate_tdm_feedback(
        routes,
        platform,
        schedule,
        feedback,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    report: Dict[str, Any] = {
        "schema": PHASE5_REPORT_SCHEMA,
        "phase": 5,
        "status": "pass",
        "design": schedule["design"],
        "platform": platform.name,
        "provider": schedule["provider"],
        **(
            {
                "optimization_provider": ratio_plan["provider"],
                "ratio_validation": ratio_validation,
            }
            if ratio_plan is not None
            else {}
        ),
        "validation": validation,
        **(
            {"timing_validation": timing_validation}
            if timing_validation is not None
            else {}
        ),
        **(
            {"candidate_selection": candidate_selection}
            if candidate_selection is not None
            else {}
        ),
        "simulation": simulation,
        "tdm_feedback_validation": feedback_validation,
        "artifacts": {
            "schedule": "schedule.json",
            "schedule_tsv": "schedule.tsv",
            "transport_manifest": "transport_manifest.json",
            "rtl_testbench": "transport_schedule_tb.sv",
            "report": "phase5_report.json",
            "tdm_feedback": "tdm_feedback.json",
        },
    }
    if exact_mode:
        report["cut_mode"] = "static-exact-combinational"
        report["qualification"] = "dependency-schedule-readiness-pass"
    if ratio_plan is not None:
        report["artifacts"]["ratio_plan"] = "ratio_plan.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    if ratio_plan is not None:
        write_json(
            output_dir / "ratio_plan.json",
            pack_managed_json(ratio_plan) if managed_storage else ratio_plan,
            compact=True,
        )
    # Phase 5 machine artifacts can contain one record per routed hop.  Using
    # compact JSON keeps the encoder on CPython's C fast path; pretty-printing
    # a million-hop schedule can otherwise dominate the actual optimizer.
    write_json(
        output_dir / "schedule.json",
        pack_managed_json(schedule) if managed_storage else schedule,
        compact=True,
    )
    (output_dir / "schedule.tsv").write_text(
        schedule_to_tsv(schedule), encoding="utf-8"
    )
    write_json(
        output_dir / "transport_manifest.json",
        pack_managed_json(manifest) if managed_storage else manifest,
        compact=True,
    )
    write_json(
        output_dir / "tdm_feedback.json",
        pack_managed_json(feedback) if managed_storage else feedback,
        compact=True,
    )
    (output_dir / "transport_schedule_tb.sv").write_text(
        schedule_to_systemverilog_testbench(
            routes,
            schedule,
            platform,
            frames=simulation_frames,
        ),
        encoding="utf-8",
    )
    write_json(output_dir / "phase5_report.json", report)
    return report


def validate_phase5(
    routes_path: Path,
    platform_path: Path,
    schedule_path: Path,
    ratio_plan_path: Optional[Path] = None,
) -> Dict[str, Any]:
    routes = read_json(routes_path)
    platform = Platform.load(platform_path)
    schedule = read_json(schedule_path)
    ratio_plan = (
        read_json(ratio_plan_path)
        if ratio_plan_path is not None
        else None
    )
    # Academic schedules are constructed and certified against the canonical
    # dense ratio model.  Rebuild that model once for standalone validation as
    # well; otherwise the schedule validator compares the native certificate
    # with the sparse baseline reconstruction, which is intentionally used
    # only for ratio-free baseline scale runs and may use a different path
    # representation.  Sharing one model also avoids rebuilding a large
    # TimingPathDB for the subsequent timing report.
    prepared_ratio_model = (
        _prepare_model(routes, platform) if ratio_plan is not None else None
    )
    validation = validate_tdm_schedule(
        routes,
        platform,
        schedule,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    if isinstance(routes.get("timing"), dict):
        validation["timing"] = reconstruct_tdm_schedule_timing(
            routes, platform, schedule, model=prepared_ratio_model
        )
    return validation
