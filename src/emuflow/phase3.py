"""Phase 3 native stage entry and independent artifact validation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ValidationError
from .io import read_json
from .partition import (DEFAULT_PARTITION_CUT_MODE, DEFAULT_PATRON_ALGORITHM_VERSION,
    CUT_MODE_STATIC_EXACT, summarize_balance_observation)
from .partition_limits import planning_resource_capacities, strict_balance_limits
from .platform import Platform
from .resources import RESOURCE_FIELDS
from .resource_budget import validate_device_resource_usage
from .phase1 import indexed_phase1_identity
from .phase3_assignment_index import validate_phase3_assignment_index_against_emuir
from .phase3_runtime import measure_phase3_runtime, phase3_runtime_call, phase3_runtime_observe
from .combinational_cut import (STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH)

PHASE3_REPORT_SCHEMA = "emuflow.phase3-report/v1"


@measure_phase3_runtime
def run_phase3(
    ir_path: Path,
    platform_path: Path,
    output_dir: Path,
    constraints_path: Optional[Path] = None,
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    provider: str = "patron",
    openroad: Optional[str] = None,
    tritonpart_solution: Optional[Path] = None,
    net_weights_path: Optional[Path] = None,
    tritonpart_timeout_seconds: int = 3600,
    tritonpart_seed_attempts: int = 1,
    tritonpart_num_initial_solutions: int = 50,
    tritonpart_num_best_initial_solutions: int = 10,
    tritonpart_repair_min_used_fpgas: bool = True,
    tritonpart_run_unweighted_baseline: bool = False,
    route_constraints_path: Optional[Path] = None,
    hop_refiner: Optional[str] = None,
    cut_mode: str = DEFAULT_PARTITION_CUT_MODE,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    timing_database_path: Optional[Path] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_algorithm_version: int = DEFAULT_PATRON_ALGORITHM_VERSION,
) -> Dict[str, Any]:
    """Run the single native producer; legacy graph and snapshot routes are retired."""
    if type(seed) is not int or seed < 0:
        raise ValidationError('partition seed: expected a non-negative integer')
    if provider not in {'patron', 'tritonpart'}:
        raise ValidationError(f'unsupported Phase 3 provider {provider!r}')
    if type(patron_algorithm_version) is not int or patron_algorithm_version != 14:
        raise ValidationError('PATRON algorithm version is invalid')
    if cut_mode != CUT_MODE_STATIC_EXACT:
        raise ValidationError('Phase 3 v14 requires generalized Static Exact mode')
    if os.environ.get('EMUFLOW_PATRON_INPUT_COPY'):
        raise ValidationError('PATRON full input-copy route is retired')
    from .emuir_compact import is_compact_emuir
    from .emuir_storage import is_emuir_sqlite
    from .native_tools import resolve_native_executable
    selected_tool = Path(resolve_native_executable('emuflow-native')).resolve()
    if any(value is not None and Path(value).expanduser().resolve() != selected_tool
           for value in (patron_refiner, hop_refiner)):
        raise ValidationError('Phase 3 requires the unified native executable')
    if not is_emuir_sqlite(Path(ir_path)) or not is_compact_emuir(Path(ir_path)):
        raise ValidationError('Phase 3 requires compact EmuIR storage; the Python graph route is retired')
    from .phase3_native import run
    options = dict(constraints_path=constraints_path, seed=seed, min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance, provider=provider, openroad=openroad,
        tritonpart_solution=tritonpart_solution, net_weights_path=net_weights_path,
        tritonpart_timeout_seconds=tritonpart_timeout_seconds,
        tritonpart_seed_attempts=tritonpart_seed_attempts,
        tritonpart_num_initial_solutions=tritonpart_num_initial_solutions,
        tritonpart_num_best_initial_solutions=tritonpart_num_best_initial_solutions,
        tritonpart_repair_min_used_fpgas=tritonpart_repair_min_used_fpgas,
        tritonpart_run_unweighted_baseline=tritonpart_run_unweighted_baseline,
        route_constraints_path=route_constraints_path,
        max_cross_fpga_dependency_depth=max_cross_fpga_dependency_depth,
        static_exact_candidate_policy=static_exact_candidate_policy,
        timing_database_path=timing_database_path, patron_max_moves=patron_max_moves)
    report = phase3_runtime_call('native.run', run, output_dir, ir_path, platform_path, **options)
    phase3_runtime_observe(static_exact_native=1, native_phase3_stage=1,
        instances=report['validation']['instances'], clusters=report['validation']['clusters'],
        cut_nets=report['validation']['cut_nets'])
    return report


def validate_phase3_indexed(
    ir_path: Path,
    platform_path: Path,
    phase3_report_path: Path,
    assignment_index_path: Path,
) -> Dict[str, Any]:
    """Validate a completed Phase 3 without replaying its Python graph.

    The Phase 3 producer already performs the exhaustive in-process artifact
    validation before publication.  A complete-flow terminal check needs an
    independent relation check, not a second construction of every EmuIR,
    cluster, endpoint, and cut object.  The assignment sidecar and compact
    EmuIR make that relation check set-oriented and bounded-memory.
    """

    platform = Platform.load(platform_path)
    design, _, _ = indexed_phase1_identity(ir_path)
    proof = validate_phase3_assignment_index_against_emuir(
        assignment_index_path,
        ir_path,
        expected_design=design,
        expected_platform=platform.name,
    )
    report = read_json(phase3_report_path)
    if (
        report.get("schema") != PHASE3_REPORT_SCHEMA
        or report.get("status") != "pass"
        or report.get("design") != design
        or report.get("platform") != platform.name
    ):
        raise ValidationError("Phase 3 report identity is invalid")
    validation = report.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "pass":
        raise ValidationError("Phase 3 report lacks passing validation")
    if validation.get("instances") != proof["source_instances"]:
        raise ValidationError("Phase 3 report instance count disagrees")
    resource_field = (
        "primary_resources_by_fpga"
        if "primary_resources_by_fpga" in validation
        else "resources_by_fpga"
    )
    if validation.get(resource_field) != proof["resources_by_fpga"]:
        raise ValidationError("Phase 3 report resource totals disagree")
    constraints = read_json(phase3_report_path.parent / "constraints.normalized.json")
    if proof.get("constraints") != constraints:
        raise ValidationError("indexed Phase 3 sealed constraints disagree or are missing")
    active = constraints.get("active_fpgas")
    fpgas = [fpga for fpga in platform.fpgas if fpga.id in (active or [])]
    if not fpgas or len(fpgas) != len(set(active)):
        raise ValidationError("indexed Phase 3 active FPGA set is invalid")
    for fpga in platform.fpgas:
        validate_device_resource_usage(
            proof["total_resources_by_fpga"][fpga.id], fpga.to_dict(), stage="indexed-phase3",
        )
    fields = ["cells", *[field for field in RESOURCE_FIELDS
        if any(counts.get(field, 0) for counts in proof["resources_by_fpga"].values())
        and all(fpga.effective_capacity.get(field, 0) > 0 for fpga in fpgas)]]
    loads = {fpga.id: {field: (proof["instances_by_fpga"][fpga.id] if field == "cells"
                              else proof["resources_by_fpga"][fpga.id].get(field, 0))
                       for field in fields} for fpga in fpgas}
    if any(proof["instances_by_fpga"].get(fpga.id, 0) for fpga in platform.fpgas if fpga.id not in active):
        raise ValidationError("indexed Phase 3 assignment uses inactive FPGA")
    totals = {field: sum(row[field] for row in loads.values()) for field in fields}
    tolerance = constraints["balance_tolerance"]
    overrides = constraints.get("balance_tolerance_by_dimension", {})
    strict_dimensions = set(
        constraints.get("balance_dimensions", fields)
    ) & set(fields)
    bounds = strict_balance_limits(
        fpgas,
        totals,
        tolerance,
        overrides,
        strict_dimensions,
        constraints.get("resource_reservations_by_fpga", {}),
    )
    expected_modes = {
        field: ("balance" if field in strict_dimensions else "capacity")
        for field in fields
    }
    for fpga in loads:
        for field in fields:
            if loads[fpga][field] > bounds[fpga][field]:
                mode = expected_modes[field]
                raise ValidationError(
                    f"indexed Phase 3 strict {mode} exceeded: {fpga} "
                    f"{field} used={loads[fpga][field]} "
                    f"allowed={bounds[fpga][field]}; automatic relaxation "
                    "is forbidden"
                )
    percentages = {field: overrides.get(field, tolerance) * 100.0 for field in fields}
    planning_capacity = planning_resource_capacities(
        fpgas, constraints.get("resource_reservations_by_fpga", {})
    )
    expected_capacity_limits = {
        fpga.id: {
            field: (
                totals[field]
                if field == "cells"
                else planning_capacity[fpga.id][field]
            )
            for field in fields
        }
        for fpga in fpgas
    }
    reservation_evidence_disagrees = bool(
        constraints.get("resource_reservations_by_fpga")
        and (
            validation.get("physical_capacity_limits")
            != {
                fpga.id: {
                    field: (
                        totals[field]
                        if field == "cells"
                        else fpga.effective_capacity[field]
                    )
                    for field in fields
                }
                for fpga in fpgas
            }
            or validation.get("resource_reservations_by_fpga")
            != {
                fpga.id: dict(
                    sorted(
                        constraints.get(
                            "resource_reservations_by_fpga", {}
                        ).get(fpga.id, {}).items()
                    )
                )
                for fpga in fpgas
            }
        )
    )
    if (validation.get("balance_auto_relaxed") is not False
        or validation.get("requested_balance_percent") != tolerance * 100.0
        or validation.get("effective_balance_percent") != max(percentages.values())
        or validation.get("effective_balance_percent_by_dimension") != percentages
        or validation.get("requested_balance_percent_by_dimension") != percentages
        or validation.get("strict_balance_dimensions") != sorted(strict_dimensions)
        or validation.get("capacity_only_dimensions") != sorted(set(fields) - strict_dimensions)
        or validation.get("constraint_mode_by_dimension") != expected_modes
        or validation.get("enforced_balance_percent_by_dimension")
            != {field: percentages[field] for field in strict_dimensions}
        or validation.get("capacity_limits") != expected_capacity_limits
        or reservation_evidence_disagrees
        or validation.get("strict_balance_violations") != 0
        or validation.get("capacity_violations") != 0
        or validation.get("balance_loads") != loads
        or validation.get("balance_allowed_loads") != bounds):
        raise ValidationError("indexed Phase 3 strict balance evidence disagrees with sealed constraints")
    # Older sealed reports predate this diagnostic field; when present it must
    # agree with the independently reconstructed indexed loads.
    if ("balance_observation" in validation and validation["balance_observation"]
        != summarize_balance_observation(loads, strict_dimensions)):
        raise ValidationError("indexed Phase 3 balance observation disagrees with loads")
    if proof["semantic_contract_sha256"] is not None:
        if validation.get("cut_mode") != CUT_MODE_STATIC_EXACT:
            raise ValidationError(
                "Phase 3 exact assignment lacks exact validation status"
            )
    elif validation.get("cut_mode") == CUT_MODE_STATIC_EXACT:
        raise ValidationError(
            "Phase 3 sequential assignment claims exact validation status"
        )
    return validation
