"""Blind application and complete-flow promotion gate for calibrated platforms."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .ppro_blackbox_calibration import validate_blackbox_observation


HOLDOUT_RESULT_SCHEMA = "emuflow.ppro-holdout-result/v1"
PROMOTION_REPORT_SCHEMA = "emuflow.ppro-platform-promotion/v1"
_TIERS = {"medium", "diversity", "large", "large_primary", "very_large_final"}


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{context}: expected a finite number")
    return result


def _nonnegative(value: Any, context: str) -> float:
    result = _number(value, context)
    if result < 0:
        raise ValidationError(f"{context}: expected a non-negative number")
    return result


def _nonnegative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a non-negative integer")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        raise ValidationError(f"{context}: expected a non-negative integer")
    return int(numeric)


def validate_holdout_result(value: Mapping[str, Any]) -> Dict[str, Any]:
    required = {"schema", "id", "workload_id", "tier", "algorithm_id", "ppro", "emuflow"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValidationError("holdout result fields are invalid")
    if value.get("schema") != HOLDOUT_RESULT_SCHEMA:
        raise ValidationError("holdout result schema is invalid")
    for name in ("id", "workload_id", "algorithm_id"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValidationError(f"holdout result {name} is invalid")
    if value["tier"] not in _TIERS:
        raise ValidationError("holdout result tier is invalid")
    ppro = validate_blackbox_observation(value["ppro"])
    if (
        ppro["identity"]["role"] != "holdout"
        or ppro["experiment"]["kind"] != "application_holdout"
        or ppro["execution"]["outcome"] != "pass"
    ):
        raise ValidationError("holdout result requires a passing PPro application holdout")
    if (
        not ppro["reports"]["route_summary"]
        or not ppro["reports"]["system_timing"]
        or not ppro["metrics"]["routes"]
    ):
        raise ValidationError("PPro holdout lacks route or system-timing evidence")
    emuflow = value["emuflow"]
    if not isinstance(emuflow, Mapping):
        raise ValidationError("holdout EmuFlow summary is invalid")
    emuflow_required = {
        "status",
        "configuration_id",
        "physical_seed",
        "resource_utilization",
        "maximum_tdm_ratio",
        "worst_cross_fpga_delay_ns",
        "busiest_pairs",
        "global_wns_ns",
        "global_tns_ns",
        "global_timing_engine",
        "phase1_7_complete",
        "macro_cycle_equivalence",
        "schedule_legality",
        "zero_unrouted_nets",
        "zero_drc_violations",
        "original_path_coverage",
    }
    if set(emuflow) != emuflow_required or emuflow.get("status") != "pass":
        raise ValidationError("holdout EmuFlow summary fields or status are invalid")
    if emuflow["configuration_id"] != ppro["identity"]["configuration_id"]:
        configuration_match = False
    else:
        configuration_match = True
    if emuflow["physical_seed"] != 1:
        raise ValidationError("holdout validation requires the single default physical seed 1")
    if emuflow["global_timing_engine"] != "opensta":
        raise ValidationError("holdout validation requires authoritative OpenSTA global timing")
    booleans = (
        "phase1_7_complete",
        "macro_cycle_equivalence",
        "schedule_legality",
        "zero_unrouted_nets",
        "zero_drc_violations",
    )
    if any(type(emuflow[name]) is not bool for name in booleans):
        raise ValidationError("holdout EmuFlow completion check is not boolean")
    coverage = _number(emuflow["original_path_coverage"], "holdout path coverage")
    if not 0.0 <= coverage <= 1.0:
        raise ValidationError("holdout path coverage is outside [0,1]")
    utilization = emuflow["resource_utilization"]
    if not isinstance(utilization, Mapping) or not utilization:
        raise ValidationError("holdout EmuFlow resource utilization is invalid")
    normalized_utilization = {
        str(name): _nonnegative(amount, f"holdout utilization {name}")
        for name, amount in sorted(utilization.items())
    }
    if any(amount > 1.0 for amount in normalized_utilization.values()):
        raise ValidationError("holdout EmuFlow utilization exceeds one")
    busiest_pairs = emuflow["busiest_pairs"]
    if not isinstance(busiest_pairs, list) or any(
        not isinstance(item, str) or "->" not in item for item in busiest_pairs
    ):
        raise ValidationError("holdout EmuFlow busiest-pair ordering is invalid")
    if len(busiest_pairs) != len(set(busiest_pairs)):
        raise ValidationError("holdout EmuFlow busiest-pair ordering contains duplicates")
    global_tns = _number(emuflow["global_tns_ns"], "holdout global TNS")
    if global_tns > 0:
        raise ValidationError("holdout global TNS must use the non-positive STA convention")
    normalized_emuflow = {
        **dict(emuflow),
        "configuration_match": configuration_match,
        "resource_utilization": normalized_utilization,
        "maximum_tdm_ratio": _nonnegative_integer(
            emuflow["maximum_tdm_ratio"], "holdout maximum TDM ratio"
        ),
        "worst_cross_fpga_delay_ns": _nonnegative(
            emuflow["worst_cross_fpga_delay_ns"], "holdout cross-FPGA delay"
        ),
        "global_wns_ns": _number(emuflow["global_wns_ns"], "holdout global WNS"),
        "global_tns_ns": global_tns,
        "original_path_coverage": coverage,
    }
    return {
        "schema": HOLDOUT_RESULT_SCHEMA,
        "id": value["id"],
        "workload_id": value["workload_id"],
        "tier": value["tier"],
        "algorithm_id": value["algorithm_id"],
        "ppro": ppro,
        "emuflow": normalized_emuflow,
    }


def _ppro_resource_utilization(observation: Mapping[str, Any]) -> Dict[str, float]:
    records = observation["metrics"]["fpga_utilization"]
    names = sorted(set().union(*(record["resources"] for record in records))) if records else []
    return {
        name: max(record["resources"].get(name, 0.0) for record in records)
        for name in names
    }


def _ppro_pair_order(observation: Mapping[str, Any]) -> list[str]:
    loads: Dict[str, float] = defaultdict(float)
    for route in observation["metrics"]["routes"]:
        for sink in route["sinks"]:
            loads[f"{route['source']}->{sink}"] += float(route["signal_count"])
    return [name for name, _ in sorted(loads.items(), key=lambda item: (-item[1], item[0]))]


def _major_order_agreement(left: Sequence[str], right: Sequence[str]) -> bool:
    if not left or not right:
        return not left and not right
    if left[0] != right[0]:
        return False
    common = [item for item in left if item in set(right)][:3]
    right_common = [item for item in right if item in set(common)]
    return common == right_common


def evaluate_holdout_promotion(results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    normalized = [validate_holdout_result(item) for item in results]
    if not normalized:
        raise ValidationError("holdout promotion requires results")
    cases = []
    for item in normalized:
        ppro = item["ppro"]
        emuflow = item["emuflow"]
        ppro_utilization = _ppro_resource_utilization(ppro)
        common_resources = sorted(set(ppro_utilization) & set(emuflow["resource_utilization"]))
        utilization_errors = {
            name: abs(ppro_utilization[name] - emuflow["resource_utilization"][name])
            for name in common_resources
        }
        if "maximum_tdm_ratio" not in ppro["metrics"]["communication"]:
            raise ValidationError("PPro holdout lacks maximum TDM ratio")
        ppro_tdm = _nonnegative_integer(
            ppro["metrics"]["communication"]["maximum_tdm_ratio"],
            "PPro holdout maximum TDM ratio",
        )
        ppro_delay = float(
            ppro["metrics"]["timing"].get("sr0_worst_cross_fpga_delay_ns", -1.0)
        )
        if ppro_delay < 0:
            raise ValidationError("PPro holdout lacks worst cross-FPGA delay")
        if ppro_delay:
            delay_error = abs(emuflow["worst_cross_fpga_delay_ns"] - ppro_delay) / ppro_delay
        else:
            delay_error = 0.0 if emuflow["worst_cross_fpga_delay_ns"] == 0.0 else 1.0
        complete = all(
            emuflow[name]
            for name in (
                "phase1_7_complete",
                "macro_cycle_equivalence",
                "schedule_legality",
                "zero_unrouted_nets",
                "zero_drc_violations",
            )
        ) and emuflow["original_path_coverage"] == 1.0
        ppro_pairs = _ppro_pair_order(ppro)
        case = {
            "id": item["id"],
            "workload_id": item["workload_id"],
            "tier": item["tier"],
            "algorithm_id": item["algorithm_id"],
            "configuration_match": emuflow["configuration_match"],
            "maximum_resource_utilization_error": max(utilization_errors.values(), default=None),
            "resource_comparison_available": bool(common_resources),
            "tdm_ratio_difference": abs(emuflow["maximum_tdm_ratio"] - ppro_tdm),
            "cross_fpga_delay_relative_error": delay_error,
            "busiest_pair_order_agrees": _major_order_agreement(
                ppro_pairs, emuflow["busiest_pairs"]
            ),
            "complete_phase1_7_gate": complete,
            "global_wns_ns": emuflow["global_wns_ns"],
            "global_tns_ns": emuflow["global_tns_ns"],
        }
        case["passes"] = (
            case["configuration_match"]
            and case["resource_comparison_available"]
            and case["maximum_resource_utilization_error"] <= 0.10
            and case["tdm_ratio_difference"] <= 1
            and case["cross_fpga_delay_relative_error"] <= 0.15
            and case["busiest_pair_order_agrees"]
            and case["complete_phase1_7_gate"]
        )
        cases.append(case)

    ranking_checks = []
    by_workload: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for case, item in zip(cases, normalized):
        by_workload[case["workload_id"]].append(
            {
                "algorithm_id": case["algorithm_id"],
                "ppro": item["ppro"]["metrics"]["timing"]["sr0_worst_cross_fpga_delay_ns"],
                "emuflow": item["emuflow"]["worst_cross_fpga_delay_ns"],
            }
        )
    for workload, items in sorted(by_workload.items()):
        if len(items) < 2:
            continue
        ppro_order = [item["algorithm_id"] for item in sorted(items, key=lambda entry: (entry["ppro"], entry["algorithm_id"]))]
        emuflow_order = [item["algorithm_id"] for item in sorted(items, key=lambda entry: (entry["emuflow"], entry["algorithm_id"]))]
        ranking_checks.append(
            {"workload_id": workload, "ppro_order": ppro_order, "emuflow_order": emuflow_order, "matches": ppro_order == emuflow_order}
        )
    tiers = {case["tier"] for case in cases if case["passes"]}
    promotion = (
        all(case["passes"] for case in cases)
        and _TIERS <= tiers
        and bool(ranking_checks)
        and all(item["matches"] for item in ranking_checks)
    )
    return {
        "schema": PROMOTION_REPORT_SCHEMA,
        "status": "pass" if promotion else "fail",
        "promoted": promotion,
        "cases": cases,
        "ranking_checks": ranking_checks,
        "covered_passing_tiers": sorted(tiers),
        "required_tiers": sorted(_TIERS),
    }
