"""Independent QoR aggregation for canonical Phase 6 provider studies."""

from __future__ import annotations

import hashlib
import math
import statistics
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .multi_fpga_physical_flow import validate_multi_fpga_physical_report
from .runtime import QOR_REPORT_SCHEMA


CANONICAL_QOR_COMPARISON_SCHEMA = "emuflow.canonical-qor-comparison/v2"
_PROVIDERS = ("baseline", "placement-aware", "chimew")
_METRICS = (
    "global_target_clock_wns_ns",
    "global_target_clock_tns_ns",
    "global_runtime_clock_wns_ns",
    "global_runtime_clock_tns_ns",
    "per_fpga_wns_ns",
    "per_fpga_tns_ns",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValidationError(
            f"canonical QoR arm artifact is missing: {relative}"
        )
    return path


def parse_canonical_qor_arms(
    records: Sequence[Sequence[str]],
) -> Dict[Tuple[str, int], Path]:
    """Parse repeated CLI ``--arm PROVIDER SEED ROOT`` triples."""

    arms: Dict[Tuple[str, int], Path] = {}
    for record in records:
        if len(record) != 3:
            raise ValidationError("canonical QoR arm must have three fields")
        provider, seed_text, root_text = record
        try:
            seed = int(seed_text)
        except ValueError as error:
            raise ValidationError("canonical QoR arm seed is invalid") from error
        key = (provider, seed)
        if provider not in _PROVIDERS or seed < 1 or key in arms:
            raise ValidationError("canonical QoR arm identity is invalid or duplicated")
        supplied_root = Path(root_text).expanduser()
        if supplied_root.is_symlink() or not supplied_root.is_dir():
            raise ValidationError("canonical QoR arm root is not a directory")
        root = supplied_root.resolve()
        arms[key] = root
    _physical_seeds(arms)
    if len(set(arms.values())) != len(arms):
        raise ValidationError("canonical QoR arm roots must be distinct")
    return arms


def _physical_seeds(
    arms: Mapping[Tuple[str, int], Any],
) -> tuple[int, ...]:
    """Return the common non-empty seed set covered by every provider."""

    seeds_by_provider = {provider: set() for provider in _PROVIDERS}
    for key in arms:
        if not isinstance(key, tuple) or len(key) != 2:
            raise ValidationError(
                "canonical QoR comparison requires one or more complete provider seed sets"
            )
        provider, seed = key
        if (
            provider not in seeds_by_provider
            or isinstance(seed, bool)
            or not isinstance(seed, int)
            or seed < 1
        ):
            raise ValidationError(
                "canonical QoR comparison requires one or more complete provider seed sets"
            )
        seeds_by_provider[provider].add(seed)
    if any(not seeds for seeds in seeds_by_provider.values()):
        raise ValidationError(
            "canonical QoR comparison requires one or more complete provider seed sets"
        )
    seed_sets = list(seeds_by_provider.values())
    if any(seeds != seed_sets[0] for seeds in seed_sets[1:]):
        raise ValidationError(
            "canonical QoR comparison requires one or more complete provider seed sets"
        )
    expected = {
        (provider, seed)
        for provider in _PROVIDERS
        for seed in seed_sets[0]
    }
    if set(arms) != expected:
        raise ValidationError(
            "canonical QoR comparison requires one or more complete provider seed sets"
        )
    return tuple(sorted(seed_sets[0]))


def _number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"canonical QoR {label} must be finite")
    return float(value)


def _clock_metrics(clock: Any, label: str) -> tuple[float, float, int]:
    if not isinstance(clock, dict):
        raise ValidationError(f"canonical QoR {label} is missing")
    wns = _number(clock.get("worst_slack_bound_ns"), f"{label} WNS")
    tns = _number(
        clock.get("total_negative_slack_bound_ns"), f"{label} TNS"
    )
    failing = clock.get("negative_slack_paths")
    if (
        tns > 1.0e-12
        or isinstance(failing, bool)
        or not isinstance(failing, int)
        or failing < 0
    ):
        raise ValidationError(f"canonical QoR {label} negative slack is invalid")
    return wns, tns, failing


def _shared_hashes(shared_root: Path) -> Dict[str, str]:
    return {
        "emuir_sha256": _sha256(
            _require(shared_root, "frontend/phase1/design.emuir.json")
        ),
        "assignment_sha256": _sha256(
            _require(shared_root, "partition/assignment.json")
        ),
        "routes_sha256": _sha256(
            _require(shared_root, "system-route/routes.json")
        ),
        "schedule_sha256": _sha256(
            _require(shared_root, "tdm/schedule.json")
        ),
    }


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"canonical QoR {label} digest is invalid")
    return value


def _effective_schedule_digest(
    report: Mapping[str, Any], common_upstream: Mapping[str, str]
) -> str:
    """Validate common inputs and return the provider-effective schedule seal."""

    frozen = report.get("frozen_upstream")
    expected_keys = {*common_upstream, "schedule_sha256"}
    if not isinstance(frozen, dict) or set(frozen) != expected_keys:
        raise ValidationError("canonical QoR Phase 7 upstream seal is malformed")
    if any(frozen.get(key) != digest for key, digest in common_upstream.items()):
        raise ValidationError("canonical QoR Phase 7 common upstream seal is broken")
    return _digest(frozen.get("schedule_sha256"), "effective Phase 6 schedule")


def _arm_record(
    provider: str,
    seed: int,
    root: Path,
    common_upstream: Mapping[str, str],
) -> Dict[str, Any]:
    report_path = _require(root, "experiment-phase7-report.json")
    summary_path = _require(root, "physical/physical-summary.json")
    physical_report_path = _require(
        root, "physical/multi-fpga-physical-flow-report.json"
    )
    qor_path = _require(root, "runtime/qor_report.json")
    report = read_json(report_path)
    qor = read_json(qor_path)
    effective_schedule_sha256 = _effective_schedule_digest(
        report, common_upstream
    )
    phase6_manifest_sha256 = _digest(
        report.get("phase6_manifest_sha256"), "effective Phase 6 manifest"
    )
    if (
        report.get("schema") != "emuflow.experiment-phase7-checkpoint/v1"
        or report.get("status") != "pass"
        or report.get("provider") != provider
        or report.get("physical_seed") != seed
        or report.get("physical_summary_sha256") != _sha256(summary_path)
        or report.get("qor_sha256") != _sha256(qor_path)
        or report.get("qor") != qor
    ):
        raise ValidationError("canonical QoR Phase 7 arm seal is broken")
    validate_multi_fpga_physical_report(read_json(physical_report_path))
    if qor.get("schema") != QOR_REPORT_SCHEMA or qor.get("status") != "pass":
        raise ValidationError("canonical QoR arm did not reach Phase 7C closure")
    timing = qor.get("timing")
    if not isinstance(timing, dict) or timing.get("status") != "pass":
        raise ValidationError("canonical QoR arm system timing did not close")
    target_wns, target_tns, target_failing = _clock_metrics(
        timing.get("target_clock"), "target clock"
    )
    runtime_wns, runtime_tns, runtime_failing = _clock_metrics(
        timing.get("runtime_clock"), "runtime clock"
    )
    physical = qor.get("physical")
    if (
        not isinstance(physical, dict)
        or physical.get("status") != "pass"
        or physical.get("unrouted_nets") != 0
        or physical.get("drc_violations") != 0
    ):
        raise ValidationError("canonical QoR physical closure is invalid")
    metrics = {
        "global_target_clock_wns_ns": target_wns,
        "global_target_clock_tns_ns": target_tns,
        "global_target_clock_failing_endpoints": target_failing,
        "global_runtime_clock_wns_ns": runtime_wns,
        "global_runtime_clock_tns_ns": runtime_tns,
        "global_runtime_clock_failing_endpoints": runtime_failing,
        "per_fpga_wns_ns": _number(
            physical.get("worst_wns_ns"), "per-FPGA worst WNS"
        ),
        "per_fpga_tns_ns": _number(
            physical.get("total_tns_ns"), "per-FPGA total TNS"
        ),
        "unrouted_nets": 0,
        "drc_violations": 0,
    }
    return {
        "provider": provider,
        "physical_seed": seed,
        "effective_phase6_schedule_sha256": effective_schedule_sha256,
        "effective_phase6_manifest_sha256": phase6_manifest_sha256,
        "artifacts": {
            "phase7_report_sha256": _sha256(report_path),
            "physical_summary_sha256": _sha256(summary_path),
            "physical_flow_report_sha256": _sha256(physical_report_path),
            "qor_sha256": _sha256(qor_path),
        },
        "system_timing_qualification": timing.get("qualification"),
        "path_exactness": timing.get("path_exactness"),
        "metrics": metrics,
    }


def _summary(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for metric in _METRICS:
        values = [float(record["metrics"][metric]) for record in records]
        result[metric] = {
            "values_by_seed": {
                str(record["physical_seed"]): float(record["metrics"][metric])
                for record in records
            },
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "minimum": min(values),
            "maximum": max(values),
        }
    return result


def _classification(values: Sequence[float], tolerance: float = 1.0e-9) -> str:
    if all(abs(value) <= tolerance for value in values):
        return "unchanged"
    if statistics.fmean(values) > tolerance and all(
        value >= -tolerance for value in values
    ):
        return "improved"
    if statistics.fmean(values) < -tolerance and all(
        value <= tolerance for value in values
    ):
        return "regressed"
    return "mixed"


def _comparison(
    provider: str,
    by_key: Mapping[Tuple[str, int], Mapping[str, Any]],
    seeds: Sequence[int],
) -> Dict[str, Any]:
    deltas = []
    for seed in seeds:
        baseline = by_key[("baseline", seed)]["metrics"]
        candidate = by_key[(provider, seed)]["metrics"]
        deltas.append(
            {
                "physical_seed": seed,
                **{
                    metric: float(candidate[metric]) - float(baseline[metric])
                    for metric in _METRICS
                },
            }
        )
    classifications = {
        metric: _classification([record[metric] for record in deltas])
        for metric in _METRICS
    }
    target_classes = {
        classifications["global_target_clock_wns_ns"],
        classifications["global_target_clock_tns_ns"],
    }
    if "mixed" in target_classes or (
        "improved" in target_classes and "regressed" in target_classes
    ):
        target_result = "mixed"
    elif "regressed" in target_classes:
        target_result = "regressed"
    elif "improved" in target_classes:
        target_result = "improved"
    else:
        target_result = "unchanged"
    return {
        "provider": provider,
        "baseline_provider": "baseline",
        "paired_seed_deltas": deltas,
        "mean_deltas": {
            metric: statistics.fmean([record[metric] for record in deltas])
            for metric in _METRICS
        },
        "metric_classification": classifications,
        "target_clock_result": target_result,
    }


def build_canonical_qor_comparison(
    shared_root: Path,
    arm_roots: Mapping[Tuple[str, int], Path],
) -> Dict[str, Any]:
    seeds = _physical_seeds(arm_roots)
    if shared_root.is_symlink() or not shared_root.is_dir():
        raise ValidationError("canonical QoR shared checkpoint is invalid")
    shared_root = shared_root.resolve()
    shared = _shared_hashes(shared_root)
    common_upstream = {
        key: shared[key]
        for key in ("emuir_sha256", "assignment_sha256", "routes_sha256")
    }
    records = [
        _arm_record(provider, seed, arm_roots[(provider, seed)], common_upstream)
        for provider in _PROVIDERS
        for seed in seeds
    ]
    by_key = {
        (record["provider"], record["physical_seed"]): record
        for record in records
    }
    design_platform = {
        (
            read_json(_require(arm_roots[key], "runtime/qor_report.json"))["design"],
            read_json(_require(arm_roots[key], "runtime/qor_report.json"))["platform"],
        )
        for key in sorted(arm_roots)
    }
    if len(design_platform) != 1:
        raise ValidationError("canonical QoR arms do not share design/platform")
    design, platform = next(iter(design_platform))
    provider_schedules = {}
    provider_manifests = {}
    for provider in _PROVIDERS:
        schedules = {
            by_key[(provider, seed)]["effective_phase6_schedule_sha256"]
            for seed in seeds
        }
        if len(schedules) != 1:
            raise ValidationError(
                "canonical QoR physical seeds do not share the provider Phase 6 schedule"
            )
        provider_schedules[provider] = next(iter(schedules))
        manifests = {
            by_key[(provider, seed)]["effective_phase6_manifest_sha256"]
            for seed in seeds
        }
        if len(manifests) != 1:
            raise ValidationError(
                "canonical QoR physical seeds do not share the provider Phase 6 manifest"
            )
        provider_manifests[provider] = next(iter(manifests))
    return {
        "schema": CANONICAL_QOR_COMPARISON_SCHEMA,
        "status": "pass",
        "design": design,
        "platform": platform,
        "qualification": (
            "single-seed-complete-phase7c-system-timing"
            if len(seeds) == 1
            else "paired-multi-seed-complete-phase7c-system-timing"
        ),
        "physical_seeds": list(seeds),
        "claim_scope": (
            "academic contest-topology/device projection; target-clock values "
            "are composed whole-design physical-plus-link/TDM bounds"
        ),
        "frozen_common_upstream": common_upstream,
        "shared_phase5_schedule_sha256": shared["schedule_sha256"],
        "provider_effective_phase6_schedule_sha256": provider_schedules,
        "provider_effective_phase6_manifest_sha256": provider_manifests,
        "arms": records,
        "provider_summary": {
            provider: _summary(
                [by_key[(provider, seed)] for seed in seeds]
            )
            for provider in _PROVIDERS
        },
        "comparisons": {
            provider: _comparison(provider, by_key, seeds)
            for provider in ("placement-aware", "chimew")
        },
    }


def run_canonical_qor_comparison(
    shared_root: Path,
    arm_roots: Mapping[Tuple[str, int], Path],
    output_dir: Path,
) -> Dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists() and (
        not output_dir.is_dir() or any(output_dir.iterdir())
    ):
        raise EmuFlowError("canonical QoR output must be an empty directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_canonical_qor_comparison(shared_root, arm_roots)
    write_json(output_dir / "canonical-qor-comparison.json", report)
    validate_canonical_qor_comparison(output_dir, shared_root, arm_roots)
    return report


def validate_canonical_qor_comparison(
    root: Path,
    shared_root: Path,
    arm_roots: Mapping[Tuple[str, int], Path],
) -> Dict[str, Any]:
    report = read_json(_require(root, "canonical-qor-comparison.json"))
    expected = build_canonical_qor_comparison(shared_root, arm_roots)
    if report != expected:
        raise ValidationError("canonical QoR comparison replay disagrees")
    return {
        "status": "pass",
        "design": report["design"],
        "platform": report["platform"],
        "arms": len(report["arms"]),
        "chimew_target_clock_result": report["comparisons"]["chimew"][
            "target_clock_result"
        ],
    }
