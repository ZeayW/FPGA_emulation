"""Choose a complete author routing/TDM pair after ordinary Phase 5 checks."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import shutil
import time
from typing import Any, Mapping, Optional

from .author_routing import AUTHOR_ORDER_POLICIES, AUTHOR_ROUTER_PROVIDER
from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5


AUTHOR_ROUTE_TDM_SCHEMA = "emuflow.author-route-tdm-selection/v1"
AUTHOR_ROUTE_TDM_POLICIES = ("author-pressure-v1", "author-sll-pressure-v1")
AUTHOR_FLOW_COST_POLICIES = (*AUTHOR_ROUTE_TDM_POLICIES, "best-of-two-v1")


def validate_author_flow_policy(provider, policy, order_policy=None):
    """Validate an explicit orchestration policy without changing defaults."""
    if policy is None and order_policy is None:
        return
    if policy is not None and (not isinstance(policy, str) or policy not in AUTHOR_FLOW_COST_POLICIES):
        raise ValidationError(f"unsupported author flow cost policy: {policy}")
    if order_policy is not None and (not isinstance(order_policy, str) or order_policy not in AUTHOR_ORDER_POLICIES):
        raise ValidationError(f"unsupported author flow order policy: {order_policy}")
    if provider != AUTHOR_ROUTER_PROVIDER:
        raise ValidationError("an author flow policy requires the author routing provider")


class _InputChanged(EmuFlowError):
    """A common-input change invalidates the comparison, not one candidate."""


def _signature(path: Path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Inputs:
    def __init__(self, paths):
        self.files, self.records = {}, {}
        hashes = {}
        for label, (path, hash_contents) in paths.items():
            alias = Path(path).absolute()
            target = alias.resolve()
            if not target.is_file():
                raise ValidationError(f"author selection input is missing: {label}")
            before = _signature(target)
            if hash_contents and target not in hashes:
                hashes[target] = _sha256(target)
            if alias.resolve() != target or _signature(target) != before:
                raise _InputChanged(f"author selection input changed while sealing: {label}")
            self.files[alias] = (label, target, before)
            self.records[label] = {"bytes": before[2], **(
                {"sha256": hashes[target]} if hash_contents else {"identity": "read-only-stat-guard"}
            )}

    def check(self):
        for path, (label, target, signature) in self.files.items():
            try:
                unchanged = path.resolve() == target and _signature(path) == signature
            except OSError:
                unchanged = False
            if not unchanged:
                raise _InputChanged(f"author selection common input changed: {label}")


def _safe_reason(error):
    # Tool errors can contain transient absolute paths. Artifact references in
    # the selection report are relative; keep error summaries equally portable.
    return re.sub(r"(?<!\w)/[^\s\"'(),;]+", "<path>", str(error))


def _objective(validation, timing_expected):
    completion = validation.get("completion_slot")
    if isinstance(completion, bool) or not isinstance(completion, int) or completion < 0:
        raise ValidationError("Phase 5 has no valid completion slot")
    timing = validation.get("timing")
    if timing is None:
        if timing_expected:
            raise ValidationError("author selection lost its complete Phase 5 timing coverage")
        return "minimum-realized-completion-slot-v1", [-completion]
    if not isinstance(timing, Mapping) or timing.get("status") != "pass":
        raise ValidationError("author selection has invalid Phase 5 timing")
    values = []
    for field in ("worst_normalized_slack", "total_negative_normalized_slack",
                  "p01_normalized_slack", "median_normalized_slack"):
        value = timing.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"author selection invalid timing metric: {field}")
        values.append(value)
    negative = timing.get("negative_slack_paths")
    if isinstance(negative, bool) or not isinstance(negative, int) or negative < 0:
        raise ValidationError("author selection invalid negative path count")
    return "realized-phase5-timing-v1", [values[0], values[1], -negative,
                                          values[2], values[3], -completion]


def _validate_pair(assignment, platform, route_dir, tdm_dir, timing_paths):
    if not (tdm_dir / "cross_layer_timing.json").is_file():
        raise ValidationError("author selection requires the Phase 5 cross-layer binding")
    phase4 = validate_phase4(assignment, platform, route_dir / "routes.json", timing_paths)
    phase5 = validate_phase5(
        route_dir / "routes.json", platform, tdm_dir / "schedule.json",
        ratio_plan_path=(tdm_dir / "ratio_plan.json") if (tdm_dir / "ratio_plan.json").is_file() else None,
        assignment_path=assignment,
    )
    if (phase4.get("status") != "pass" or phase5.get("status") != "pass"
            or phase5.get("cross_layer_timing", {}).get("status") != "pass"):
        raise ValidationError("author route/TDM pair did not pass independent checks")
    return phase4, phase5


def run_author_route_tdm(
    assignment_path: Path,
    platform_path: Path,
    route_output_dir: Path,
    tdm_output_dir: Path,
    *,
    route_options: Optional[Mapping[str, Any]] = None,
    tdm_options: Optional[Mapping[str, Any]] = None,
):
    """Run both author policies and promote one unmodified P4/P5 pair.

    Options are ordinary run_phase4/run_phase5 keyword arguments. Provider and
    cost policy belong to this coordinator; assignment/platform/output paths
    are bound by the explicit arguments. Canonical output directories must be
    absent siblings. Rejected attempts and a portable receipt remain in their
    sibling ``author-route-tdm`` study directory for the caller's lifecycle.
    """
    coordinator_started = time.monotonic()
    assignment_path, platform_path = Path(assignment_path), Path(platform_path)
    route_output_dir, tdm_output_dir = Path(route_output_dir), Path(tdm_output_dir)
    if route_output_dir.parent.resolve() != tdm_output_dir.parent.resolve():
        raise ValidationError("author selection canonical outputs must be siblings")
    root = route_output_dir.parent.resolve()
    route_output_dir, tdm_output_dir = root / route_output_dir.name, root / tdm_output_dir.name
    study = root / "author-route-tdm"
    if len({route_output_dir.resolve(), tdm_output_dir.resolve(), study.resolve()}) != 3:
        raise ValidationError("author selection output directories overlap")
    if any(path.exists() or path.is_symlink() for path in (route_output_dir, tdm_output_dir)):
        raise EmuFlowError("author selection canonical outputs must not exist")
    if study.is_symlink() or (study.exists() and any(study.iterdir())):
        raise EmuFlowError("author selection study directory must be empty")

    route_options, tdm_options = dict(route_options or {}), dict(tdm_options or {})
    if "author_cost_policy" in route_options:
        raise ValidationError("author selection controls both cost policies")
    provider = route_options.pop("provider", AUTHOR_ROUTER_PROVIDER)
    if provider != AUTHOR_ROUTER_PROVIDER:
        raise ValidationError("author selection cannot use a different Phase 4 provider")
    for field in ("assignment_path", "platform_path", "output_dir"):
        if field in route_options:
            raise ValidationError(f"author selection binds route option {field}")
    if "assignment_path" in tdm_options:
        tdm_assignment = tdm_options.pop("assignment_path")
        if tdm_assignment is not None and Path(tdm_assignment).resolve() != assignment_path.resolve():
            raise ValidationError("author selection Phase 5 assignment differs")
    for field in ("routes_path", "platform_path", "output_dir"):
        if field in tdm_options:
            raise ValidationError(f"author selection binds TDM option {field}")

    paths = {"assignment": (assignment_path, True), "platform": (platform_path, True)}
    for field, value in route_options.items():
        if field.endswith("_path") and value is not None:
            paths[f"route.{field}"] = (Path(value), True)
    # Phase 3 owns this potentially large index. Guard its immutable identity
    # without adding another full database hash to every downstream stage.
    index = assignment_path.with_name("assignment.sqlite")
    if index.is_file():
        paths["phase3_index"] = (index, False)
    for field, value in {**route_options, **tdm_options}.items():
        if field in {"router", "ratio_optimizer", "timing_dag_optimizer", "slot_optimizer"} and value is not None:
            path = Path(value)
            if path.is_file():
                paths[f"tool.{field}"] = (path, False)
    inputs = _Inputs(paths)
    timing_paths = route_options.get("timing_paths_path")
    study.mkdir(parents=True, exist_ok=True)
    report = {"schema": AUTHOR_ROUTE_TDM_SCHEMA, "status": "running",
              "selection_policy": "best-of-two-v1", "provider": AUTHOR_ROUTER_PROVIDER,
              "candidate_order": list(AUTHOR_ROUTE_TDM_POLICIES),
              "tie_break": "author-pressure-v1", "common_inputs": inputs.records,
              "candidates": []}
    report_path = study / "selection.json"
    write_json(report_path, report)
    successful = []
    binary_identity = None
    for policy in AUTHOR_ROUTE_TDM_POLICIES:
        inputs.check()
        candidate_dir = study / policy
        route_dir, tdm_dir = candidate_dir / "system-route", candidate_dir / "tdm"
        record = {"cost_policy": policy, "status": "running",
                  "route_dir": route_dir.relative_to(root).as_posix(),
                  "tdm_dir": tdm_dir.relative_to(root).as_posix()}
        report["candidates"].append(record)
        write_json(report_path, report)
        started = time.monotonic()
        active_timer = None
        try:
            active_timer = "phase4_seconds"
            stamp = time.monotonic()
            phase4 = run_phase4(
                assignment_path, platform_path, route_dir,
                provider=AUTHOR_ROUTER_PROVIDER, author_cost_policy=policy, **route_options,
            )
            record["phase4_seconds"] = time.monotonic() - stamp
            active_timer = None
            inputs.check()
            if (phase4.get("status") != "pass" or phase4.get("provider") != AUTHOR_ROUTER_PROVIDER
                    or phase4.get("author_routing", {}).get("cost_policy") != policy):
                raise ValidationError("author candidate Phase 4 identity/status differs")
            identity = phase4["author_routing"]["executable_sha256"]
            if binary_identity is not None and identity != binary_identity:
                raise _InputChanged("author native binary identity changed between candidates")
            binary_identity = identity
            active_timer = "phase5_seconds"
            stamp = time.monotonic()
            phase5 = run_phase5(
                route_dir / "routes.json", platform_path, tdm_dir,
                assignment_path=assignment_path, **tdm_options,
            )
            record["phase5_seconds"] = time.monotonic() - stamp
            active_timer = None
            inputs.check()
            if phase5.get("status") != "pass":
                raise ValidationError("author candidate Phase 5 reported non-pass")
            active_timer = "validation_seconds"
            stamp = time.monotonic()
            checked4, checked5 = _validate_pair(
                assignment_path, platform_path, route_dir, tdm_dir, timing_paths,
            )
            record["validation_seconds"] = time.monotonic() - stamp
            active_timer = None
            inputs.check()
            objective, score = _objective(checked5, timing_paths is not None)
            if successful and objective != successful[0]["objective"]:
                raise _InputChanged("author candidates have different objective/coverage contracts")
            record.update(status="pass", objective=objective, score=score,
                          phase4_validation=checked4, phase5_validation=checked5,
                          artifacts={name: _sha256(path) for name, path in {
                              "routes": route_dir / "routes.json", "schedule": tdm_dir / "schedule.json",
                              "cross_layer_timing": tdm_dir / "cross_layer_timing.json",
                              **({"ratio_plan": tdm_dir / "ratio_plan.json"} if (tdm_dir / "ratio_plan.json").is_file() else {}),
                          }.items()})
            successful.append({"record": record, "objective": objective, "score": score,
                               "route_dir": route_dir, "tdm_dir": tdm_dir})
        except _InputChanged:
            raise
        except (EmuFlowError, ValidationError) as error:
            record.update(status="rejected", error_type=type(error).__name__, reason=_safe_reason(error))
        finally:
            if active_timer is not None:
                record[active_timer] = time.monotonic() - stamp
            record["wall_seconds"] = time.monotonic() - started
            write_json(report_path, report)
        inputs.check()
    if not successful:
        report.update(status="failed", wall_seconds=time.monotonic() - coordinator_started)
        write_json(report_path, report)
        raise EmuFlowError("both author route/TDM candidates failed; see author-route-tdm/selection.json")
    # max() keeps the first item for exact ties, hence continuous pressure wins.
    selected = max(successful, key=lambda candidate: candidate["score"])
    report.update(status="promoting", objective=selected["objective"],
                  selected_cost_policy=selected["record"]["cost_policy"],
                  selected_score=selected["score"],
                  route_dir=route_output_dir.relative_to(root).as_posix(),
                  tdm_dir=tdm_output_dir.relative_to(root).as_posix())
    write_json(report_path, report)
    shutil.copytree(selected["route_dir"], route_output_dir)
    shutil.copytree(selected["tdm_dir"], tdm_output_dir)
    checked4, checked5 = _validate_pair(
        assignment_path, platform_path, route_output_dir, tdm_output_dir, timing_paths,
    )
    inputs.check()
    objective, score = _objective(checked5, timing_paths is not None)
    if objective != selected["objective"] or score != selected["score"]:
        raise ValidationError("promoted author route/TDM objective changed")
    # Cross-layer validation rejects mismatched pairs. Byte identities also
    # reject replacing both members with a different, equally scoring pair.
    promoted = {"routes": route_output_dir / "routes.json", "schedule": tdm_output_dir / "schedule.json",
                "cross_layer_timing": tdm_output_dir / "cross_layer_timing.json",
                "ratio_plan": tdm_output_dir / "ratio_plan.json"}
    for name, digest in selected["record"]["artifacts"].items():
        if _sha256(promoted[name]) != digest:
            raise ValidationError("promoted author route/TDM artifact identity changed")
    report.update(status="pass", promoted_phase4_validation=checked4, promoted_phase5_validation=checked5,
                  wall_seconds=time.monotonic() - coordinator_started)
    write_json(report_path, report)
    return {"phase4": read_json(route_output_dir / "phase4_report.json"),
            "phase5": read_json(tdm_output_dir / "phase5_report.json"), "selection": report}
